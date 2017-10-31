import tensorflow as tf

import ops


class EstimatorWithBaseline(object):

    def make_baseline(self):
        res = None, []
        _make_baseline = getattr(self, '_make_baseline', None)
        if _make_baseline is not None:
            res = _make_baseline()

        return res

    def _make_baseline_train_step(self, opt, loss, baseline, baseline_vars):
        baseline_target = tf.stop_gradient(loss)

        self.baseline_loss = .5 * tf.reduce_mean(tf.square(baseline_target - baseline))
        tf.summary.scalar('baseline_loss', self.baseline_loss)
        train_step = opt.minimize(self.baseline_loss, var_list=baseline_vars)
        return train_step


class NVILEstimator(EstimatorWithBaseline):

    decay_rate = None

    def _make_train_step(self, make_opt, rec_loss, kl_div):

        # loss used as a proxy for gradient computation
        self.proxy_loss = rec_loss.value + kl_div.value + self._l2_loss()

        # REINFORCE
        reinforce_imp_weight = rec_loss.per_sample
        if not self.analytic_kl_expectation:
            reinforce_imp_weight += kl_div.per_sample

        self.baseline, self.baseline_vars = self.make_baseline()
        self.reinforce_loss = self._reinforce(reinforce_imp_weight, self.decay_rate)
        self.proxy_loss += self.reinforce_loss

        opt = make_opt(self.learning_rate)
        gvs = opt.compute_gradients(self.proxy_loss, var_list=self.model_vars)

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        global_step = tf.train.get_or_create_global_step()
        with tf.control_dependencies(update_ops):
            train_step = opt.apply_gradients(gvs, global_step=global_step)

        if self.baseline is not None:
            baseline_opt = make_opt(10 * self.learning_rate)
            self._baseline_train_step = self._make_baseline_train_step(baseline_opt, reinforce_imp_weight,
                                                                       self.baseline, self.baseline_vars)
            train_step = tf.group(train_step, self._baseline_train_step)
        return train_step, gvs

    def _reinforce(self, importance_weight, decay_rate):
        """Implements REINFORCE for training the discrete probability distribution over number of steps and train-step
         for the baseline"""

        log_prob = self.num_steps_posterior.log_prob(self.num_step_per_sample)

        if self.baseline is not None:
               importance_weight -= self.baseline

        # constant baseline and learning signal normalisation according to NVIL paper
        if decay_rate is not None:
            axes = range(len(importance_weight.get_shape()))
            mean, var = tf.nn.moments(tf.squeeze(importance_weight), axes=axes)
            self.imp_weight_moving_mean = ops.make_moving_average('imp_weight_moving_mean', mean, 0., decay_rate)
            self.imp_weight_moving_var = ops.make_moving_average('imp_weight_moving_var', var, 1., decay_rate)

            factor = tf.maximum(tf.sqrt(self.imp_weight_moving_var), 1.)
            importance_weight = (importance_weight - self.imp_weight_moving_mean) / factor

        self.importance_weight = importance_weight
        axes = range(len(self.importance_weight.get_shape()))
        imp_weight_mean, imp_weight_var = tf.nn.moments(self.importance_weight, axes)
        tf.summary.scalar('imp_weight_mean', imp_weight_mean)
        tf.summary.scalar('imp_weight_var', imp_weight_var)
        reinforce_loss_per_sample = tf.stop_gradient(self.importance_weight) * log_prob
        reinforce_loss = tf.reduce_mean(reinforce_loss_per_sample)
        tf.summary.scalar('reinforce_loss', reinforce_loss)

        return reinforce_loss


class ImportanceWeightedNVILEstimator(EstimatorWithBaseline):

    decay_rate = None
    importance_resample = False
    use_r_imp_weight = True

    def _make_nelbo(self):
        return self.nelbo

    def _iw_resample(self, *args):
        iw_sample_idx = self.iw_distrib.sample()
        iw_sample_idx += tf.range(self.batch_size) * self.iw_samples
        resampled = [tf.gather(arg, iw_sample_idx) for arg in args]
        if len(resampled) == 1:
            resampled = resampled[0]

        return resampled

    def _estimate_importance_weighted_elbo(self, per_sample_elbo):

        per_sample_elbo = tf.reshape(per_sample_elbo, (self.batch_size, self.iw_samples))
        importance_weights = tf.nn.softmax(per_sample_elbo, -1)
        self.iw_distrib = tf.contrib.distributions.Categorical(per_sample_elbo)

        biggest = tf.reduce_max(per_sample_elbo, -1, keep_dims=True)
        normalised = tf.exp(per_sample_elbo - biggest)
        elbo = tf.log(tf.reduce_sum(normalised, -1, keep_dims=True)) + biggest - tf.log(float(self.iw_samples))
        return elbo, importance_weights

    def _make_train_step(self, make_opt, rec_loss, kl_div):

        negative_per_sample_elbo = rec_loss.per_sample + kl_div.per_sample
        per_sample_elbo = -negative_per_sample_elbo
        iw_elbo_estimate, elbo_importance_weights = self._estimate_importance_weighted_elbo(per_sample_elbo)

        self.elbo_importance_weights = tf.stop_gradient(elbo_importance_weights)

        self.negative_weighted_per_sample_elbo = self.elbo_importance_weights \
                                            * tf.reshape(negative_per_sample_elbo, (self.batch_size, self.iw_samples))

        # loss used as a proxy for gradient computation
        self.baseline, self.baseline_vars = self.make_baseline()

        # TODO: run 4 full tests: with r_imp_weight set to 0 and not, with resampling and not

        posterior_num_steps_log_prob = self.num_steps_posterior.log_prob(self.num_step_per_sample)
        if self.importance_resample:
            posterior_num_steps_log_prob = self._iw_resample(posterior_num_steps_log_prob)
            posterior_num_steps_log_prob = tf.reshape(posterior_num_steps_log_prob, (self.batch_size, 1))
            r_imp_weight = 1.
        else:
            posterior_num_steps_log_prob = tf.reshape(posterior_num_steps_log_prob, (self.batch_size, self.iw_samples))
            r_imp_weight = self.elbo_importance_weights

        if not self.use_r_imp_weight:
            r_imp_weight = 0.

        self.nelbo_per_sample = -tf.reshape(iw_elbo_estimate, (self.batch_size, 1))
        num_steps_learning_signal = self.nelbo_per_sample
        self.nelbo = tf.reduce_mean(self.nelbo_per_sample)
        self.proxy_loss = self.nelbo + self._l2_loss()

        self.reinforce_loss = self._reinforce(num_steps_learning_signal + r_imp_weight, posterior_num_steps_log_prob)
        self.proxy_loss += self.reinforce_loss

        opt = make_opt(self.learning_rate)
        gvs = opt.compute_gradients(self.proxy_loss, var_list=self.model_vars)

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        global_step = tf.train.get_or_create_global_step()
        with tf.control_dependencies(update_ops):
            train_step = opt.apply_gradients(gvs, global_step=global_step)

        if self.baseline is not None:
            baseline_opt = make_opt(10 * self.learning_rate)
            self._baseline_train_step = self._make_baseline_train_step(baseline_opt, num_steps_learning_signal,
                                                                       self.baseline, self.baseline_vars)
            train_step = tf.group(train_step, self._baseline_train_step)
        return train_step, gvs

    def _reinforce(self, learning_signal, posterior_num_steps_log_prob):
        """Implements REINFORCE for training the discrete probability distribution over number of steps and train-step
         for the baseline"""

        self.num_steps_learning_signal = learning_signal
        if self.baseline is not None:
            self.num_steps_learning_signal -= self.baseline

        # constant baseline and learning signal normalisation according to NVIL paper
        if self.decay_rate is not None:
            axes = range(len(learning_signal.get_shape()))
            mean, var = tf.nn.moments(tf.squeeze(learning_signal), axes=axes)
            self.imp_weight_moving_mean = ops.make_moving_average('imp_weight_moving_mean', mean, 0., self.decay_rate)
            self.imp_weight_moving_var = ops.make_moving_average('imp_weight_moving_var', var, 1., self.decay_rate)

            factor = tf.maximum(tf.sqrt(self.imp_weight_moving_var), 1.)
            self.num_steps_learning_signal = (self.num_steps_learning_signal - self.imp_weight_moving_mean) / factor

        axes = range(len(self.num_steps_learning_signal.get_shape()))
        imp_weight_mean, imp_weight_var = tf.nn.moments(self.num_steps_learning_signal, axes)
        tf.summary.scalar('imp_weight_mean', imp_weight_mean)
        tf.summary.scalar('imp_weight_var', imp_weight_var)
        reinforce_loss_per_sample = tf.stop_gradient(self.num_steps_learning_signal) * posterior_num_steps_log_prob

        shape = reinforce_loss_per_sample.shape.as_list()
        assert len(shape) == 2 and shape[0] == self.batch_size and shape[1] in (1, self.iw_samples), 'shape is {}'.format(shape)

        reinforce_loss = tf.reduce_mean(tf.reduce_sum(reinforce_loss_per_sample, -1))
        tf.summary.scalar('reinforce_loss', reinforce_loss)

        return reinforce_loss



class ResampledImportanceWeightedNVILEstimator(object):
    # TODO: implement
    pass


class VIMCOEstimator(object):
    # TODO: implement
    pass