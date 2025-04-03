# Copyright 2024 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Class to subsample minibatches by balancing positives and negatives.

Subsamples minibatches based on a pre-specified positive fraction in range
[0,1]. The class presumes there are many more negatives than positive examples:
if the desired batch_size cannot be achieved with the pre-specified positive
fraction, it fills the rest with negative examples. If this is not sufficient
for obtaining the desired batch_size, it returns fewer examples.

The main function to call is Subsample(self, indicator, labels). For convenience
one can also call SubsampleWeights(self, weights, labels) which is defined in
the minibatch_sampler base class.

When is_static is True, it implements a method that guarantees static shapes.
It also ensures the length of output of the subsample is always batch_size, even
when number of examples set to True in indicator is less than batch_size.

This is originally implemented in TensorFlow Object Detection API.
"""

import tensorflow as tf
import tensorflow.keras as tf_keras


def combined_static_and_dynamic_shape(tensor):
  """Returns a list containing static and dynamic values for the dimensions.

  Returns a list of static and dynamic values for shape dimensions. This is
  useful to preserve static shapes when available in reshape operation.

  Args:
    tensor: A tensor of any type.

  Returns:
    A list of size tensor.shape.ndims containing integers or a scalar tensor.
  """
  static_tensor_shape = tensor.shape.as_list()
  dynamic_tensor_shape = tf.shape(input=tensor)
  combined_shape = []
  for index, dim in enumerate(static_tensor_shape):
    if dim is not None:
      combined_shape.append(dim)
    else:
      combined_shape.append(dynamic_tensor_shape[index])
  return combined_shape


def indices_to_dense_vector(indices,
                            size,
                            indices_value=1.,
                            default_value=0,
                            dtype=tf.float32):
  """Creates dense vector with indices set to specific value and rest to zeros.

  This function exists because it is unclear if it is safe to use
    tf.sparse_to_dense(indices, [size], 1, validate_indices=False)
  with indices which are not ordered.
  This function accepts a dynamic size (e.g. tf.shape(tensor)[0])

  Args:
    indices: 1d Tensor with integer indices which are to be set to
        indices_values.
    size: scalar with size (integer) of output Tensor.
    indices_value: values of elements specified by indices in the output vector
    default_value: values of other elements in the output vector.
    dtype: data type.

  Returns:
    dense 1D Tensor of shape [size] with indices set to indices_values and the
        rest set to default_value.
  """
  size = tf.cast(size, dtype=tf.int32)
  zeros = tf.ones([size], dtype=dtype) * default_value
  values = tf.ones_like(indices, dtype=dtype) * indices_value

  return tf.dynamic_stitch(
      [tf.range(size), tf.cast(indices, dtype=tf.int32)], [zeros, values])


def matmul_gather_on_zeroth_axis(params, indices, scope=None):
  """Matrix multiplication based implementation of tf.gather on zeroth axis.

  TODO(rathodv, jonathanhuang): enable sparse matmul option.

  Args:
    params: A float32 Tensor. The tensor from which to gather values.
      Must be at least rank 1.
    indices: A Tensor. Must be one of the following types: int32, int64.
      Must be in range [0, params.shape[0])
    scope: A name for the operation (optional).

  Returns:
    A Tensor. Has the same type as params. Values from params gathered
    from indices given by indices, with shape indices.shape + params.shape[1:].
  """
  scope = scope or 'MatMulGather'
  with tf.name_scope(scope):
    params_shape = combined_static_and_dynamic_shape(params)
    indices_shape = combined_static_and_dynamic_shape(indices)
    params2d = tf.reshape(params, [params_shape[0], -1])
    indicator_matrix = tf.one_hot(indices, params_shape[0])
    gathered_result_flattened = tf.matmul(indicator_matrix, params2d)
    return tf.reshape(gathered_result_flattened,
                      tf.stack(indices_shape + params_shape[1:]))


class BalancedPositiveNegativeSampler:
  """Subsamples minibatches to a desired balance of positives and negatives."""

  def __init__(self, positive_fraction=0.5, is_static=False):
    """Constructs a minibatch sampler.

    Args:
      positive_fraction: desired fraction of positive examples (scalar in [0,1])
        in the batch.
      is_static: If True, uses an implementation with static shape guarantees.

    Raises:
      ValueError: if positive_fraction < 0, or positive_fraction > 1
    """
    if positive_fraction < 0 or positive_fraction > 1:
      raise ValueError('positive_fraction should be in range [0,1]. '
                       'Received: %s.' % positive_fraction)
    self._positive_fraction = positive_fraction
    self._is_static = is_static

  @staticmethod
  def subsample_indicator(indicator, num_samples):
    """Subsample indicator vector.

    Given a boolean indicator vector with M elements set to `True`, the function
    assigns all but `num_samples` of these previously `True` elements to
    `False`. If `num_samples` is greater than M, the original indicator vector
    is returned.

    Args:
      indicator: a 1-dimensional boolean tensor indicating which elements
        are allowed to be sampled and which are not.
      num_samples: int32 scalar tensor

    Returns:
      a boolean tensor with the same shape as input (indicator) tensor
    """
    indices = tf.where(indicator)
    indices = tf.random.shuffle(indices)
    indices = tf.reshape(indices, [-1])

    num_samples = tf.minimum(tf.size(input=indices), num_samples)
    selected_indices = tf.slice(indices, [0], tf.reshape(num_samples, [1]))

    selected_indicator = indices_to_dense_vector(
        selected_indices,
        tf.shape(input=indicator)[0])

    return tf.equal(selected_indicator, 1)

  def _get_num_pos_neg_samples(self, sorted_indices_tensor, sample_size):
    """Counts the number of positives and negatives numbers to be sampled.

    Args:
      sorted_indices_tensor: A sorted int32 tensor of shape [N] which contains
        the signed indices of the examples where the sign is based on the label
        value. The examples that cannot be sampled are set to 0. It samples
        at most sample_size*positive_fraction positive examples and remaining
        from negative examples.
      sample_size: Size of subsamples.

    Returns:
      A tuple containing the number of positive and negative labels in the
      subsample.
    """
    input_length = tf.shape(input=sorted_indices_tensor)[0]
    valid_positive_index = tf.greater(sorted_indices_tensor,
                                      tf.zeros(input_length, tf.int32))
    num_sampled_pos = tf.reduce_sum(
        input_tensor=tf.cast(valid_positive_index, tf.int32))
    max_num_positive_samples = tf.constant(
        int(sample_size * self._positive_fraction), tf.int32)
    num_positive_samples = tf.minimum(max_num_positive_samples, num_sampled_pos)
    num_negative_samples = tf.constant(sample_size,
                                       tf.int32) - num_positive_samples

    return num_positive_samples, num_negative_samples

  def _get_values_from_start_and_end(self, input_tensor, num_start_samples,
                                     num_end_samples, total_num_samples):
    """slices num_start_samples and last num_end_samples from input_tensor.

    Args:
      input_tensor: An int32 tensor of shape [N] to be sliced.
      num_start_samples: Number of examples to be sliced from the beginning
        of the input tensor.
      num_end_samples: Number of examples to be sliced from the end of the
        input tensor.
      total_num_samples: Sum of is num_start_samples and num_end_samples. This
        should be a scalar.

    Returns:
      A tensor containing the first num_start_samples and last num_end_samples
      from input_tensor.

    """
    input_length = tf.shape(input=input_tensor)[0]
    start_positions = tf.less(tf.range(input_length), num_start_samples)
    end_positions = tf.greater_equal(
        tf.range(input_length), input_length - num_end_samples)
    selected_positions = tf.logical_or(start_positions, end_positions)
    selected_positions = tf.cast(selected_positions, tf.float32)
    indexed_positions = tf.multiply(tf.cumsum(selected_positions),
                                    selected_positions)
    one_hot_selector = tf.one_hot(tf.cast(indexed_positions, tf.int32) - 1,
                                  total_num_samples,
                                  dtype=tf.float32)
    return tf.cast(tf.tensordot(tf.cast(input_tensor, tf.float32),
                                one_hot_selector, axes=[0, 0]), tf.int32)

  def _static_subsample(self, indicator, batch_size, labels):
    """Returns subsampled minibatch.

    Args:
      indicator: boolean tensor of shape [N] whose True entries can be sampled.
        N should be a complie time constant.
      batch_size: desired batch size. This scalar cannot be None.
      labels: boolean tensor of shape [N] denoting positive(=True) and negative
        (=False) examples. N should be a complie time constant.

    Returns:
      sampled_idx_indicator: boolean tensor of shape [N], True for entries which
        are sampled. It ensures the length of output of the subsample is always
        batch_size, even when number of examples set to True in indicator is
        less than batch_size.

    Raises:
      ValueError: if labels and indicator are not 1D boolean tensors.
    """
    # Check if indicator and labels have a static size.
    if not indicator.shape.is_fully_defined():
      raise ValueError('indicator must be static in shape when is_static is'
                       'True')
    if not labels.shape.is_fully_defined():
      raise ValueError('labels must be static in shape when is_static is'
                       'True')
    if not isinstance(batch_size, int):
      raise ValueError('batch_size has to be an integer when is_static is'
                       'True.')

    input_length = tf.shape(input=indicator)[0]

    # Set the number of examples set True in indicator to be at least
    # batch_size.
    num_true_sampled = tf.reduce_sum(
        input_tensor=tf.cast(indicator, tf.float32))
    additional_false_sample = tf.less_equal(
        tf.cumsum(tf.cast(tf.logical_not(indicator), tf.float32)),
        batch_size - num_true_sampled)
    indicator = tf.logical_or(indicator, additional_false_sample)

    # Shuffle indicator and label. Need to store the permutation to restore the
    # order post sampling.
    permutation = tf.random.shuffle(tf.range(input_length))
    indicator = matmul_gather_on_zeroth_axis(
        tf.cast(indicator, tf.float32), permutation)
    labels = matmul_gather_on_zeroth_axis(
        tf.cast(labels, tf.float32), permutation)

    # index (starting from 1) when indicator is True, 0 when False
    indicator_idx = tf.where(
        tf.cast(indicator, tf.bool), tf.range(1, input_length + 1),
        tf.zeros(input_length, tf.int32))

    # Replace -1 for negative, +1 for positive labels
    signed_label = tf.where(
        tf.cast(labels, tf.bool), tf.ones(input_length, tf.int32),
        tf.scalar_mul(-1, tf.ones(input_length, tf.int32)))
    # negative of index for negative label, positive index for positive label,
    # 0 when indicator is False.
    signed_indicator_idx = tf.multiply(indicator_idx, signed_label)
    sorted_signed_indicator_idx = tf.nn.top_k(
        signed_indicator_idx, input_length, sorted=True).values

    [num_positive_samples,
     num_negative_samples] = self._get_num_pos_neg_samples(
         sorted_signed_indicator_idx, batch_size)

    sampled_idx = self._get_values_from_start_and_end(
        sorted_signed_indicator_idx, num_positive_samples,
        num_negative_samples, batch_size)

    # Shift the indices to start from 0 and remove any samples that are set as
    # False.
    sampled_idx = tf.abs(sampled_idx) - tf.ones(batch_size, tf.int32)
    sampled_idx = tf.multiply(
        tf.cast(tf.greater_equal(sampled_idx, tf.constant(0)), tf.int32),
        sampled_idx)

    sampled_idx_indicator = tf.cast(
        tf.reduce_sum(
            input_tensor=tf.one_hot(sampled_idx, depth=input_length), axis=0),
        tf.bool)

    # project back the order based on stored permutations
    reprojections = tf.one_hot(permutation, depth=input_length,
                               dtype=tf.float32)
    return tf.cast(tf.tensordot(
        tf.cast(sampled_idx_indicator, tf.float32),
        reprojections, axes=[0, 0]), tf.bool)

  def subsample(self, indicator, batch_size, labels, scope=None):
    """Returns subsampled minibatch.

    Args:
      indicator: boolean tensor of shape [N] whose True entries can be sampled.
      batch_size: desired batch size. If None, keeps all positive samples and
        randomly selects negative samples so that the positive sample fraction
        matches self._positive_fraction. It cannot be None is is_static is True.
      labels: boolean tensor of shape [N] denoting positive(=True) and negative
          (=False) examples.
      scope: name scope.

    Returns:
      sampled_idx_indicator: boolean tensor of shape [N], True for entries which
        are sampled.

    Raises:
      ValueError: if labels and indicator are not 1D boolean tensors.
    """
    if len(indicator.get_shape().as_list()) != 1:
      raise ValueError('indicator must be 1 dimensional, got a tensor of '
                       'shape %s' % indicator.get_shape())
    if len(labels.get_shape().as_list()) != 1:
      raise ValueError('labels must be 1 dimensional, got a tensor of '
                       'shape %s' % labels.get_shape())
    if labels.dtype != tf.bool:
      raise ValueError('labels should be of type bool. Received: %s' %
                       labels.dtype)
    if indicator.dtype != tf.bool:
      raise ValueError('indicator should be of type bool. Received: %s' %
                       indicator.dtype)
    scope = scope or 'BalancedPositiveNegativeSampler'
    with tf.name_scope(scope):
      if self._is_static:
        return self._static_subsample(indicator, batch_size, labels)

      else:
        # Only sample from indicated samples
        negative_idx = tf.logical_not(labels)
        positive_idx = tf.logical_and(labels, indicator)
        negative_idx = tf.logical_and(negative_idx, indicator)

        # Sample positive and negative samples separately
        if batch_size is None:
          max_num_pos = tf.reduce_sum(
              input_tensor=tf.cast(positive_idx, dtype=tf.int32))
        else:
          max_num_pos = int(self._positive_fraction * batch_size)
        sampled_pos_idx = self.subsample_indicator(positive_idx, max_num_pos)
        num_sampled_pos = tf.reduce_sum(
            input_tensor=tf.cast(sampled_pos_idx, tf.int32))
        if batch_size is None:
          negative_positive_ratio = (
              1 - self._positive_fraction) / self._positive_fraction
          max_num_neg = tf.cast(
              negative_positive_ratio *
              tf.cast(num_sampled_pos, dtype=tf.float32),
              dtype=tf.int32)
        else:
          max_num_neg = batch_size - num_sampled_pos
        sampled_neg_idx = self.subsample_indicator(negative_idx, max_num_neg)

        return tf.logical_or(sampled_pos_idx, sampled_neg_idx)
