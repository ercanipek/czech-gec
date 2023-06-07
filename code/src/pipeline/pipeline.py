# %%
import argparse
import sys
sys.path.append('..')

# %%
import os
import tensorflow as tf

from transformers import TFAutoModelForSeq2SeqLM
from transformers import AutoTokenizer
from transformers import AutoConfig
import json

from tensorflow.keras import mixed_precision

from utils import load_data
from utils import introduce_errors 
from utils import dataset_utils

from components import losses

from multiprocessing import Process, Manager

def main(config_filename: str):
    # %%
    with open(config_filename) as json_file:
        config = json.load(json_file)

    SEED = config['seed']

    # data loading
    DATA_PATHS = config['data_paths']
    NUM_PARALLEL = config['num_parallel']
    MAX_LENGTH = config['max_length']
    SHUFFLE_BUFFER = config['shuffle_buffer']
    BUCKET_BOUNDARIES = config['bucket_boundaries']
    BUCKET_BATCH_SIZES_PER_GPU = config['bucket_batch_sizes_per_gpu']

    # model
    MODEL = config['model']
    TOKENIZER = config['tokenizer']
    FROM_CONFIG = config['from_config']
    STEPS_PER_EPOCH = config['steps_per_epoch']
    EPOCHS = config['epochs']
    USE_F16 = config['use_f16']

    # optimizer
    OPTIMIZER_NAME = config['optimizer']['name']
    OPTIMIZER_PARAMS = config['optimizer']['params']

    # loss
    LOSS = config['loss']

    # GEL config
    LANG = config['lang']
    TOKEN_FILE = config['token_file']
    TOKEN_ERR_DISTRIBUTION = config['token_err_distribution']
    CHAR_ERR_DISTRIBUTION = config['char_err_distribution']
    TOKEN_ERR_PROB = config['token_err_prob']   
    CHAR_ERR_PROB = config['char_err_prob']

    # logs
    LOG_FILE = config['log_file']
    PROFILE_BATCH = config['profile_batch']
    MODEL_CHECKPOINT_PATH = config['model_checkpoint_path']
    BACKUP_DIR =  config['backup_dir']

    # %%
    tf.random.set_seed(SEED)

    # %%
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)

    # %%
    tokens = introduce_errors.get_token_vocabulary(TOKEN_FILE)
    characters = introduce_errors.get_char_vocabulary(LANG)

    # %%
    strategy = tf.distribute.MirroredStrategy()
    num_div = strategy.num_replicas_in_sync
    print('Number of devices: %d' % num_div)

    bucket_batch_sizes = [bucket_batch_size * num_div for bucket_batch_size in BUCKET_BATCH_SIZES_PER_GPU]

    # %%
    # loading of dataset:
    manager = Manager()
    queue = manager.Queue(4 * NUM_PARALLEL)
    gel = load_data.GenereteErrorLine(
            tokens, characters, LANG, 
            TOKEN_ERR_DISTRIBUTION, CHAR_ERR_DISTRIBUTION, 
            TOKEN_ERR_PROB, CHAR_ERR_PROB)

    process = Process(
                target=load_data.data_generator, 
                args=(queue, DATA_PATHS, NUM_PARALLEL, gel, tokenizer, MAX_LENGTH,))

    process.start()

    dataset = tf.data.Dataset.from_generator(
        lambda: iter(queue.get, None),
        output_types={
                    "input_ids": tf.int32,
                    "attention_mask": tf.int32,
                    "labels": tf.int32,
                    "decoder_input_ids": tf.int32
                },
        output_shapes={
                    "input_ids": (None, ),
                    "attention_mask": (None, ),
                    "labels": (None, ),
                    "decoder_input_ids": (None, )
                })

    dataset = dataset.map(dataset_utils.split_features_and_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.shuffle(SHUFFLE_BUFFER)
    dataset = dataset.bucket_by_sequence_length(
            element_length_func=lambda x, y: tf.shape(x['input_ids'])[0],
            bucket_boundaries=BUCKET_BOUNDARIES,
            bucket_batch_sizes=bucket_batch_sizes
    )
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    # %%
    if USE_F16:
        policy = mixed_precision.Policy('mixed_float16')
        mixed_precision.set_global_policy(policy)

    # %%
    with strategy.scope():
        if OPTIMIZER_NAME == 'Adam':
            if 'clipvalue' in OPTIMIZER_PARAMS:
                print("Use clipping...")
                optimizer = tf.keras.optimizers.Adam(learning_rate=OPTIMIZER_PARAMS['lr'], clipvalue=OPTIMIZER_PARAMS['clipvalue'], global_clipnorm=OPTIMIZER_PARAMS['global_clipnorm'])
            else:
                optimizer = tf.keras.optimizers.Adam(learning_rate=OPTIMIZER_PARAMS['lr'])
        elif OPTIMIZER_NAME == 'AdamW':
            optimizer = tf.keras.optimizers.experimental.AdamW(learning_rate=OPTIMIZER_PARAMS['lr'])
        elif OPTIMIZER_NAME == 'Adafactor':
            if 'clipvalue' in OPTIMIZER_PARAMS:
                print("Use clipping...")
                optimizer = tf.keras.optimizers.experimental.Adafactor(learning_rate=OPTIMIZER_PARAMS['lr'], clipvalue=OPTIMIZER_PARAMS['clipvalue'], global_clipnorm=OPTIMIZER_PARAMS['global_clipnorm'])
        elif OPTIMIZER_NAME == 'AdaptiveAdam':
            class LRSchedule(tf.keras.optimizers.schedules.LearningRateSchedule):
                def __init__(self, warmup_steps, d_model):
                    self.warmup_steps = tf.cast(warmup_steps, tf.float32)
                    self.d_model = tf.cast(d_model, tf.float32)

                def __call__(self, step):
                    step = tf.cast(step, tf.float32)
                    lr = (1.0/tf.math.sqrt(self.d_model)) * tf.math.minimum(1.0 / tf.math.sqrt(step), (1.0 / tf.math.sqrt(self.warmup_steps)) * ((1.0 * step) / self.warmup_steps))
                    return lr

            lr = LRSchedule(OPTIMIZER_PARAMS['warmup_steps'], MAX_LENGTH)
            beta1 = OPTIMIZER_PARAMS['beta1']
            beta2 = OPTIMIZER_PARAMS['beta2']
            epsilon = OPTIMIZER_PARAMS['epsilon']
            optimizer = tf.keras.optimizers.Adam(
                learning_rate=lr,
                beta_1=beta1,
                beta_2=beta2,
                epsilon=epsilon)

    with strategy.scope(): 
        loss = None   
        if LOSS == "SCC":
            loss = losses.MaskedSparseCategoricalCrossEntropy()


    # %%
    with strategy.scope():
        if FROM_CONFIG:
            config = AutoConfig.from_pretrained(MODEL)
            model = TFAutoModelForSeq2SeqLM.from_config(config)
        else:
            model = TFAutoModelForSeq2SeqLM.from_pretrained(MODEL)

        if loss:
            model.compile(optimizer=optimizer, loss=loss)
        else:
            model.compile(optimizer=optimizer)

    # %% [markdown]
    # ---
    # ### Callbacks

    # %%
    model_checkpoint = tf.keras.callbacks.ModelCheckpoint(
        filepath=os.path.join(MODEL_CHECKPOINT_PATH, 'ckpt-{epoch}/'),
        save_weights_only=True,
        save_freq="epoch")

    # %%
    backup = tf.keras.callbacks.BackupAndRestore(
        backup_dir=BACKUP_DIR
        )

    # %%
    profiler = tf.keras.callbacks.TensorBoard(
        log_dir=LOG_FILE, 
        profile_batch=PROFILE_BATCH)

    # %%
    callbacks = [
        model_checkpoint,
        backup,
        profiler
    ]

    # %% [markdown]
    # ---

    # %% [markdown]
    # ### Train

    # %%
    if USE_F16:
        model.model.encoder.embed_scale = tf.cast(model.model.encoder.embed_scale, tf.float16)
        model.model.decoder.embed_scale = tf.cast(model.model.decoder.embed_scale, tf.float16)

    # %%
    if STEPS_PER_EPOCH:
        model.fit(dataset, callbacks=callbacks, epochs=EPOCHS, steps_per_epoch=STEPS_PER_EPOCH)
    else:
        model.fit(dataset, callbacks=callbacks, epochs=EPOCHS)

    # %% [markdown]
    # ---