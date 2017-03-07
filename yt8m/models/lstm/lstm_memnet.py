import math

import tensorflow as tf
from tensorflow.python.ops import variable_scope
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import math_ops
from tensorflow.contrib.rnn.python.ops import core_rnn_cell_impl
from tensorflow.python.util import nest

import tensorflow.contrib.slim as slim

from tensorflow.contrib.rnn.python.ops import core_rnn_cell
from tensorflow.contrib.legacy_seq2seq.python.ops import seq2seq as seq2seq_lib

from yt8m.models import models
import yt8m.models.model_utils as utils

linear = core_rnn_cell_impl._linear  # pylint: disable=protected-access

def _extract_argmax_and_embed(embedding,
                              output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts the previous symbol and embeds it.

  Args:
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.

  Returns:
    A loop function.
  """

  def loop_function(prev, _):
    if output_projection is not None:
      prev = nn_ops.xw_plus_b(prev, output_projection[0], output_projection[1])
    prev_symbol = math_ops.argmax(prev, 1)
    # Note that gradients will not propagate through the second parameter of
    # embedding_lookup.
    emb_prev = embedding_ops.embedding_lookup(embedding, prev_symbol)
    if not update_embedding:
      emb_prev = array_ops.stop_gradient(emb_prev)
    return emb_prev

  return loop_function

def attention_decoder(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False):
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    # TODO
    attention_vec_size = 100 #attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      # TODO
      # x = linear([inp] + attns, input_size, True)
      x = linear([inp], input_size, True)
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          attns = attention(state)
      else:
        attns = attention(state)

      with variable_scope.variable_scope("AttnOutputProjection"):
        # output = linear([cell_output] + attns, output_size, True)
        output = linear(attns, output_size, True)
      if loop_function is not None:
        prev = output
      outputs.append(output)

  return outputs, state


def embedding_attention_decoder(decoder_inputs,
                                initial_state,
                                attention_states,
                                cell,
                                num_symbols,
                                embedding_size,
                                num_heads=1,
                                output_size=None,
                                output_projection=None,
                                feed_previous=False,
                                update_embedding_for_previous=True,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False):
  if output_size is None:
    output_size = cell.output_size
  if output_projection is not None:
    proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(
      scope or "embedding_attention_decoder", dtype=dtype) as scope:

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])
    loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs
    ]
    return attention_decoder(
        emb_inp,
        initial_state,
        attention_states,
        cell,
        output_size=output_size,
        num_heads=num_heads,
        loop_function=loop_function,
        initial_state_attention=initial_state_attention)

class LSTMMemNet(models.BaseModel):
  def __init__(self):
    super(LSTMMemNet, self).__init__()

    self.normalize_input = False
    self.clip_global_norm = 5
    self.var_moving_average_decay = 0.9997
    self.optimizer_name = "AdamOptimizer"
    self.base_learning_rate = 3e-4

    self.max_steps = 300
    self.num_max_labels = 1

  def create_model(self, model_input, vocab_size, num_frames,
                   is_training=True, sparse_labels=None, label_weights=None,
                   input_weights=None,
                   **unused_params):
    input_size = 1024 + 128
    self.cell_size = input_size
    num_frames = tf.cast(tf.expand_dims(num_frames, 1), tf.float32)
    # model_input = utils.SampleRandomSequence(model_input, num_frames,
                                             # self.max_steps)
    input_weights = tf.tile(
        tf.expand_dims(input_weights, 2),
        [1, 1, input_size])
    model_input = model_input * input_weights

    init_state = tf.reduce_sum(model_input, axis=1) / num_frames
    dec_cell = core_rnn_cell.GRUCell(self.cell_size)
    sparse_labels = tf.reshape(sparse_labels, [-1])
    if is_training:
      outputs, _ = embedding_attention_decoder([sparse_labels], initial_state=init_state,
                                               attention_states=model_input,
                                               cell=dec_cell,
                                               num_symbols=vocab_size,
                                               embedding_size=512,
                                               num_heads=1,
                                               output_size=vocab_size,
                                               output_projection=None,
                                               feed_previous=False,
                                               dtype=tf.float32,
                                               scope="LSTMMemNet")
      logits = outputs[0]
      loss = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=sparse_labels, logits=logits)
      loss = tf.reduce_mean(loss)
      predictions = tf.nn.softmax(logits)
    else:
      loss = tf.constant(0.0)
      runtime_batch_size = tf.shape(model_input)[0]
      first = True
      with variable_scope.variable_scope("LSTMMemNet", dtype=tf.float32):
        with variable_scope.variable_scope("attention_decoder", dtype=tf.float32):
          with variable_scope.variable_scope("AttnOutputProjection"):
            '''
            input_weights = tf.reduce_sum(input_weights, axis=1)
            model_input = tf.reduce_sum(model_input, axis=1) / input_weights
            output = linear([model_input], vocab_size, True)
            predictions = tf.nn.softmax(output)
            '''
            preds = []
            for num_splits in [1, 3, 6, 12, 25, 30, 60, 100]:
              splits = tf.split(model_input, num_or_size_splits=num_splits, axis=1)
              splits_weights = tf.split(input_weights, num_or_size_splits=num_splits, axis=1)
              for idx, split in enumerate(splits):
                weight = splits_weights[idx]
                nf = tf.reduce_sum(weight, axis=1)
                nsum = tf.reduce_sum(nf, axis=1)
                safe_sentinel = tf.ones((runtime_batch_size, input_size))

                safe_nf = tf.where(tf.equal(nsum, 0), x=nf, y=safe_sentinel)

                split = tf.reduce_sum(split, axis=1) / safe_nf
                if not first:
                  print(idx)
                  tf.get_variable_scope().reuse_variables()
                  first = False
                output = linear([split], vocab_size, True)
                pred = tf.nn.softmax(output)
                pred = pred * tf.tile(nf[:, 0:1], [1, vocab_size])
                preds.append(pred)
            predictions = tf.add_n(preds)
    return {
        "predictions": predictions,
        "loss": loss,
    }
