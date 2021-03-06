import tensorflow as tf
from tensorflow import app
from tensorflow import gfile
from tensorflow import logging
from tensorflow import flags

from yt8m.models import losses
from yt8m.starter import frame_level_models
from yt8m.starter import video_level_models
from yt8m.models.lstm import lstm
from yt8m.models.lstm import lstm_enc_dec
from yt8m.models.lstm import skip_thought
from yt8m.models.lstm import lstm_memnet
from yt8m.models.lstm import h3gru
from yt8m.models.lstm import gru_attn_new
from yt8m.models.lstm import ln_h_lstm
from yt8m.models.lstm import bi_h_lstm
from yt8m.models.lstm import bi_h_lstm_new
from yt8m.models.lstm import h_lstm
from yt8m.models.lstm import stack_gru
from yt8m.models.attn import attn_models
from yt8m.models.clockwork import clockwork
from yt8m.models.skip import gru_2_skip_3_random_dropout
from yt8m.models.context import context
from yt8m.models.fusion import fusion
from yt8m.models.label_bias import binary_cls
from yt8m.models.noisy_label import noisy_label
from yt8m.models.vlad import prune_cls
from yt8m.models.dilated import dilation
from yt8m.models.dilated import dilation_model
from yt8m.models.convgru import convGRU
from yt8m.models.randomsequence import randomseq
from yt8m.models.netvlad import netvlad
from yt8m.data_io import readers
from yt8m.data_io import vlad_reader
from yt8m.data_io import hdfs_reader
from yt8m.data_io import hdfs_reader_bias
from yt8m.data_io import hdfs_reader_no_bias
import utils
from .config import base as base_config
import models.conv.train as conv_train
import train_loop
import eval_loop
import inference_loop

FLAGS = flags.FLAGS

flags.DEFINE_string("stage", "train", "")
flags.DEFINE_string("model_ckpt_path", "", "")
flags.DEFINE_string("config_name", "BaseConfig", "")

class Expr(object):
  def __init__(self):
    self.stage = FLAGS.stage
    self.model_ckpt_path = FLAGS.model_ckpt_path
    self.config = utils.find_class_by_name(FLAGS.config_name,
                                           [base_config,])(self.stage)
    self.phase_train = self.config.phase_train
    self.task = 0
    self.ps_tasks = 0
    self.is_chief = (self.task == 0)
    self.master = ""

    self.batch_size = self.config.batch_size

    if not self.phase_train:
      tf.set_random_seed(0)

    self.model = utils.find_class_by_name(self.config.model_name,
        [frame_level_models, video_level_models, lstm, lstm_enc_dec, skip_thought,
         lstm_memnet, conv_train, binary_cls, dilation, netvlad, noisy_label, prune_cls,
         h_lstm, fusion, h3gru, gru_attn_new, ln_h_lstm, bi_h_lstm, bi_h_lstm_new,
         dilation_model, convGRU, randomseq, stack_gru, context,
         gru_2_skip_3_random_dropout, clockwork, attn_models])()
    self.label_loss_fn = utils.find_class_by_name(
        self.config.label_loss, [losses])()
    self.optimizer = utils.find_class_by_name(
        self.model.optimizer_name, [tf.train])

    # convert feature_names and feature_sizes to lists of values
    self.feature_names, self.feature_sizes = utils.GetListOfFeatureNamesAndSizes(
        self.config.feature_names, self.config.feature_sizes)
    if self.config.use_hdfs:
      inputs = hdfs_reader_no_bias.enqueue_data(
          self.config.input_feat_type, self.phase_train, self.batch_size,
          self.model.num_classes, sum(self.feature_sizes))
      video_id_batch, dense_labels_batch, model_input_raw = inputs
      sparse_labels_batch, num_frames, label_weights_batch = None, None, None
      input_weights_batch = None
      inputs = video_id_batch, model_input_raw, dense_labels_batch, \
               sparse_labels_batch, num_frames, label_weights_batch, \
               input_weights_batch
    else:
      inputs = self.get_input_data_tensors(
          self.config.data_pattern,
          num_readers=self.config.num_readers,
          num_epochs=self.config.num_epochs)

    self.build_graph(inputs)
    logging.info("built graph")
    init_fn = self.model.get_train_init_fn()

    if self.model.var_moving_average_decay > 0:
      print("Using moving average")
      variable_averages = tf.train.ExponentialMovingAverage(
          self.model.var_moving_average_decay)
      variables_to_restore = variable_averages.variables_to_restore()
      eval_saver = tf.train.Saver(variables_to_restore)
    else:
      eval_saver = tf.train.Saver(tf.global_variables())

    if self.stage == "train":
      train_loop.train_loop(self, self.model_ckpt_path, init_fn=init_fn)
    elif self.stage == "eval":
      eval_loop.evaluation_loop(self, eval_saver, self.model_ckpt_path)
    elif self.stage == "inference":
      inference_loop.inference_loop(self, eval_saver, self.model_ckpt_path)

  def get_input_data_tensors(self,
                             data_pattern,
                             num_epochs=None,
                             num_readers=1):
    reader = self.get_reader()
    logging.info("Using batch size of " + str(self.batch_size) + ".")
    with tf.name_scope("model_input"):
      files = gfile.Glob(data_pattern)
      if not files:
        raise IOError("Unable to find files. data_pattern='" +
                      data_pattern + "'")
      logging.info("number of training files: " + str(len(files)))
      filename_queue = tf.train.string_input_producer(
          files, shuffle=self.phase_train, num_epochs=num_epochs)
      data = [
          reader.prepare_reader(filename_queue) for _ in xrange(num_readers)]

      if self.phase_train:
        return tf.train.shuffle_batch_join(
            data,
            batch_size=self.batch_size,
            capacity=self.batch_size * 10,
            min_after_dequeue=self.batch_size * 5,
            allow_smaller_final_batch=True,
            enqueue_many=True)
      else:
        return tf.train.batch_join(
            data,
            batch_size=self.batch_size,
            capacity=self.batch_size * 3,
            allow_smaller_final_batch=True,
            enqueue_many=True)

  def get_reader(self):
    if self.config.input_feat_type == "frame":
      reader = readers.YT8MFrameFeatureReader(
          num_classes=self.model.num_classes,
          feature_names=self.feature_names,
          num_max_labels=self.model.num_max_labels,
          feature_sizes=self.feature_sizes)
    elif self.config.input_feat_type == "video":
      reader = readers.YT8MAggregatedFeatureReader(
          num_classes=self.model.num_classes,
          feature_names=self.feature_names,
          num_max_labels=self.model.num_max_labels,
          feature_sizes=self.feature_sizes,)
          # label_smoothing=self.config.label_smoothing)
    elif self.config.input_feat_type == "vlad":
      reader = vlad_reader.YT8MVLADFeatureReader(
          feature_names=self.feature_names,
          feature_sizes=self.feature_sizes)
    elif self.config.input_feat_type == "score":
      reader = readers.YT8MScoreFeatureReader(
          num_classes=self.model.num_classes,
          feature_names=self.feature_names,
          num_max_labels=self.model.num_max_labels,
          feature_sizes=self.feature_sizes,)
    elif self.config.input_feat_type == "555":
      reader = readers.YT8M555FeatureReader(
          num_classes=self.model.num_classes,
          feature_names=self.feature_names,
          num_max_labels=self.model.num_max_labels,
          feature_sizes=self.feature_sizes,)
    return reader

  def build_graph(self, inputs):
    with tf.device(tf.train.replica_device_setter(
        self.ps_tasks, merge_devices=True)):
      self.global_step = tf.Variable(0, trainable=False, name="global_step", dtype=tf.int64)
      self.global_step1 = tf.Variable(0, trainable=False, name="global_step1", dtype=tf.int64)

      video_id_batch, model_input_raw, dense_labels_batch, sparse_labels_batch, num_frames, label_weights_batch, input_weights_batch = inputs
      feature_dim = len(model_input_raw.get_shape()) - 1

      if self.model.normalize_input:
        print("L2 Normalizing input")
        model_input = tf.nn.l2_normalize(model_input_raw, feature_dim)
      else:
        model_input = model_input_raw
      # TODO
      if self.model.num_classes == 1000:
        logging.info("num classes: 1000")
        dense_labels_batch = dense_labels_batch[:, :1000]
        # sparse_labels_batch = sparse_labels_batch[:, :1000]
      if self.model.num_classes == 1001:
        logging.info("num classes: 1001")
        dense_labels_batch = dense_labels_batch[:, 1000: 2001]
      if self.model.num_classes == 3000:
        logging.info("num classes: 3000")
        dense_labels_batch = dense_labels_batch[:, 1000: 4000]
      dense_labels_batch = tf.cast(dense_labels_batch, tf.float32)
      # dense_labels_batch = tf.Print(dense_labels_batch, [tf.reduce_sum(dense_labels_batch, 1)])
      with tf.name_scope("model"):
        result = self.model.create_model(
            model_input,
            num_frames=num_frames,
            vocab_size=self.model.num_classes,
            dense_labels=dense_labels_batch,
            sparse_labels=sparse_labels_batch,
            label_weights=label_weights_batch,
            is_training=self.phase_train,
            label_smoothing=self.config.label_smoothing,
            input_weights=input_weights_batch,
            feature_sizes=self.feature_sizes)

        predictions = result["predictions"]
        if "loss" in result.keys():
          label_loss = result["loss"]
        else:
          label_loss = self.label_loss_fn.calculate_loss(predictions, dense_labels_batch)

      if self.stage == "train":
        train_op, train_op1, label_loss, global_norm = train_loop.get_train_op(self, result, label_loss)
        self.feed_out = {
            "train_op": train_op,
            "loss": label_loss,
            "global_step": self.global_step,
            "predictions": predictions,
            "dense_labels": dense_labels_batch,
            "global_norm": global_norm,
        }
        self.feed_out1 = {
            "train_op1": train_op1,
            "loss": label_loss,
            "global_step": self.global_step1,
            "predictions": predictions,
            "dense_labels": dense_labels_batch,
            "global_norm": global_norm,
        }
      elif self.stage == "eval":
        self.feed_out = {
          "video_id": video_id_batch,
          "predictions": predictions,
          "dense_labels": dense_labels_batch,
          "loss": label_loss
        }
        if "feats" in result.keys():
          self.feed_out['feats'] = result['feats']
      elif self.stage == "inference":
        self.feed_out = {
          "video_id": video_id_batch,
          "predictions": predictions,
        }


def main(unused_argv):
  logging.set_verbosity(tf.logging.INFO)
  print("tensorflow version: %s" % tf.__version__)
  Expr()

if __name__ == "__main__":
  app.run()
