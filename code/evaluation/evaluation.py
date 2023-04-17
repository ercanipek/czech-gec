# %%
import sys
sys.path.append('..')

# %%
import os
import tensorflow as tf
from create_errors import introduce_errors
import aspell

from transformers import TFAutoModelForSeq2SeqLM, DataCollatorForSeq2Seq 
from transformers import TFEncoderDecoderModel, BertTokenizer
from transformers import AutoTokenizer, AutoConfig

import json

from m2scorer.scripts.util import paragraphs
from m2scorer.scripts.util import smart_open
from m2scorer.scripts.levenshtein import batch_multi_pre_rec_f1

# %%
with open('config.json') as json_file:
    config = json.load(json_file)

# %%
USE_MODEL = config['model']
DATA_PATHS = config['data_paths']
NUM_PARALLEL = config['num_parallel']
BATCH_SIZE_PER_REPLICE = config['batch_size_per_replica']
MAX_LENGTH = config['max_length']
STEPS_PER_EPOCH = config['steps_per_epoch']
EPOCHS = config['epochs']
SHUFFLE_BUFFER = config['shuffle_buffer']

# %%
print(f"Batch size per replica: {BATCH_SIZE_PER_REPLICE}")
strategy = tf.distribute.MirroredStrategy()
print('Number of devices: %d' % strategy.num_replicas_in_sync)
BATCH_SIZE = BATCH_SIZE_PER_REPLICE * strategy.num_replicas_in_sync

# %%
optimizer_name = config['optimizer']['name']
optimizer_params = config['optimizer']['params']

with strategy.scope():
    if optimizer_name == 'Adam':
        optimizer = tf.keras.optimizers.Adam(learning_rate=optimizer_params['lr'])
    elif optimizer_name == 'AdamW':
        optimizer = tf.keras.optimizers.experimental.AdamW(learning_rate=optimizer_params['lr'])
    elif optimizer_name == 'Adafactor':
        optimizer = tf.keras.optimizers.experimental.Adafactor(learning_rate=optimizer_params['lr'])
    elif optimizer_name == 'AdaptiveAdam':
        class LRSchedule(tf.keras.optimizers.schedules.LearningRateSchedule):
            def __init__(self, warmup_steps, d_model):
                self.warmup_steps = tf.cast(warmup_steps, tf.float32)
                self.d_model = tf.cast(d_model, tf.float32)

            def __call__(self, step):
                step = tf.cast(step, tf.float32)
                lr = (1.0/tf.math.sqrt(self.d_model)) * tf.math.minimum(1.0 / tf.math.sqrt(step), (1.0 / tf.math.sqrt(self.warmup_steps)) * ((1.0 * step) / self.warmup_steps))
                return lr

        lr = LRSchedule(optimizer_params['warmup_steps'], MAX_LENGTH)
        beta1 = optimizer_params['beta1']
        beta2 = optimizer_params['beta2']
        epsilon = optimizer_params['epsilon']
        optimizer = tf.keras.optimizers.Adam(
            learning_rate=lr,
            beta_1=beta1,
            beta_2=beta2,
            epsilon=epsilon)

with strategy.scope(): 
    loss = None   
    if config['loss'] == "SCC":
        loss = tf.keras.losses.SparseCategoricalCrossentropy()

# %%
lang = config['lang']
token_file = config['token_file']
tokens = introduce_errors.get_token_vocabulary(token_file)
characters = introduce_errors.get_char_vocabulary(lang)
aspell_speller = aspell.Speller('lang', lang)
token_err_distribution = config['token_err_distribution']
char_err_distribution = config['char_err_distribution']
token_err_prob = config['token_err_prob']   
char_err_prob = config['char_err_prob']

# %%
tokenizer = AutoTokenizer.from_pretrained(config['model'])

# %%
class GenereteErrorLine():

    def __init__(self, tokens, characters, aspell_speller, token_err_distribution, char_err_distribution, token_err_prob, char_err_prob, token_std_dev=0.2, char_std_dev=0.01):
        self.tokens = tokens
        self.characters = characters
        self.aspell_speller = aspell_speller
        self.token_err_distribution = token_err_distribution
        self.char_err_distribution = char_err_distribution
        self.token_err_prob = token_err_prob
        self.token_std_dev = token_std_dev
        self.char_err_prob = char_err_prob
        self.char_std_dev = char_std_dev

    def __call__(self, line):
        line = line.decode('utf-8')
        token_replace_prob, token_insert_prob, token_delete_prob, token_swap_prob, recase_prob = self.token_err_distribution
        char_replace_prob, char_insert_prob, char_delete_prob, char_swap_prob, change_diacritics_prob = self.char_err_distribution
        line = line.strip('\n')
        
        # introduce word-level errors
        line = introduce_errors.introduce_token_level_errors_on_sentence(line.split(' '), token_replace_prob, token_insert_prob, token_delete_prob,
                                                        token_swap_prob, recase_prob, float(self.token_err_prob), float(self.token_std_dev),
                                                        self.tokens, self.aspell_speller)
        if '\t' in line or '\n' in line:
            raise ValueError('!!! Error !!! ' + line)
        # introduce spelling errors
        line = introduce_errors.introduce_char_level_errors_on_sentence(line, char_replace_prob, char_insert_prob, char_delete_prob, char_swap_prob,
                                                       change_diacritics_prob, float(self.char_err_prob), float(self.char_std_dev),
                                                       self.characters)
        return line
    
gel = GenereteErrorLine(tokens, characters, aspell_speller, token_err_distribution, char_err_distribution, token_err_prob, char_err_prob)

# %%
def get_tokenized_sentences(error_line, label_line):
    error_line = error_line.decode('utf-8')
    label_line = label_line.decode('utf-8')
    tokenized = tokenizer(error_line, text_target=label_line, max_length=MAX_LENGTH, padding='max_length', truncation=True, return_tensors="tf")
    return tokenized['input_ids'], tokenized['attention_mask'], tokenized['labels']

def create_error_line(line):
    error_line = tf.numpy_function(gel, inp=[line], Tout=[tf.string])[0]
    label_line = line
    input_ids, attention_mask, labels = tf.numpy_function(get_tokenized_sentences, inp=[error_line, label_line], Tout=[tf.int32, tf.int32, tf.int32])
    decoder_input_ids = tf.roll(labels, shift=1, axis=1)
    dato = {
        'input_ids': input_ids[0],
        'attention_mask': attention_mask[0],
        'decoder_input_ids': decoder_input_ids[0],
        'labels': labels[0]
    }
    return dato

def ensure_shapes(input_dict):
    return {key: tf.ensure_shape(val, (MAX_LENGTH)) for key, val in input_dict.items()}

def split_features_and_labels(input_batch):
    features = {key: tensor for key, tensor in input_batch.items() if key in ['input_ids', 'attention_mask', 'decoder_input_ids']}
    labels = {key: tensor for key, tensor in input_batch.items() if key in ['labels']}
    if len(features) == 1:
        features = list(features.values())[0]
    if len(labels) == 1:
        labels = list(labels.values())[0]
    if isinstance(labels, dict) and len(labels) == 0:
        return features
    else:
        return features, labels


# %%
dataset = tf.data.TextLineDataset(DATA_PATHS, num_parallel_reads=NUM_PARALLEL)

dataset = dataset.map(create_error_line, num_parallel_calls=tf.data.experimental.AUTOTUNE)
dataset = dataset.map(ensure_shapes, num_parallel_calls=tf.data.experimental.AUTOTUNE)
dataset = dataset.map(split_features_and_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)
dataset = dataset.ignore_errors()
dataset = dataset.shuffle(SHUFFLE_BUFFER)
dataset = dataset.batch(BATCH_SIZE)
dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

# %%
with strategy.scope():
    if config["pretrained"]:
        model = TFAutoModelForSeq2SeqLM.from_pretrained(config['model'])
    else:
        config = AutoConfig.from_pretrained(config['model'])
        model = TFAutoModelForSeq2SeqLM.from_config(config)
    
    if loss:
        model.compile(optimizer=optimizer, loss=loss)
    else:
        model.compile(optimizer=optimizer)

# %% [markdown]
# ---
# ### Evaluation

# %%
max_unchanged_words=2
beta = 0.5
ignore_whitespace_casing= False
verbose = False
very_verbose = False

dev_input = config['evaluation_input']
dev_gold = config['evaluation_gold']

# load source sentences and gold edits
fin = smart_open(dev_input, 'r')
dev_input_sentences = [line.strip() for line in fin.readlines()]
fin.close()

def load_annotation(gold_file):
    source_sentences = []
    gold_edits = []
    fgold = smart_open(gold_file, 'r')
    puffer = fgold.read()
    fgold.close()
    # puffer = puffer.decode('utf8')
    for item in paragraphs(puffer.splitlines(True)):
        item = item.splitlines(False)
        sentence = [line[2:].strip() for line in item if line.startswith('S ')]
        assert sentence != []
        annotations = {}
        for line in item[1:]:
            if line.startswith('I ') or line.startswith('S '):
                continue
            assert line.startswith('A ')
            line = line[2:]
            fields = line.split('|||')
            start_offset = int(fields[0].split()[0])
            end_offset = int(fields[0].split()[1])
            etype = fields[1]
            if etype == 'noop':
                start_offset = -1
                end_offset = -1
            corrections =  [c.strip() if c != '-NONE-' else '' for c in fields[2].split('||')]
            # NOTE: start and end are *token* offsets
            original = ' '.join(' '.join(sentence).split()[start_offset:end_offset])
            annotator = int(fields[5])
            if annotator not in list(annotations.keys()):
                annotations[annotator] = []
            annotations[annotator].append((start_offset, end_offset, original, corrections))
        tok_offset = 0
        for this_sentence in sentence:
            tok_offset += len(this_sentence.split())
            source_sentences.append(this_sentence)
            this_edits = {}
            for annotator, annotation in annotations.items():
                this_edits[annotator] = [edit for edit in annotation if edit[0] <= tok_offset and edit[1] <= tok_offset and edit[0] >= 0 and edit[1] >= 0]
            if len(this_edits) == 0:
                this_edits[0] = []
            gold_edits.append(this_edits)
    return (source_sentences, gold_edits)

dev_source_sentences, dev_gold_edits = load_annotation(dev_gold)

# %%
class Evaluation(tf.keras.callbacks.Callback):
    def __init__(self, tokenizer, nth, max_unchanged_words, beta, ignore_whitespace_casing, verbose, very_verbose, 
                 dev_input_sentences, dev_source_sentences, dev_gold_edits):
        self.tokenzer = tokenizer
        self.nth = nth
        self.max_unchanged_words = max_unchanged_words
        self.beta = beta
        self.ignore_whitespace_casing = ignore_whitespace_casing
        self.verbose = verbose
        self.very_verbose = very_verbose
        self.dev_input_sentences = dev_input_sentences
        self.dev_source_sentences = dev_source_sentences
        self.dev_gold_edits = dev_gold_edits

    def on_epoch_end(self, epoch, logs=None):
        if epoch % self.nth == 0:
            try:
                predicted_sentences = []
                for sentence in self.dev_input_sentences: 
                    tokenized_sentence = tokenizer(sentence, max_length=MAX_LENGTH, padding='max_length', truncation=True, return_tensors="tf")
                    output = model.generate(tokenized_sentence['input_ids'])
                    predicted_sentence =  tokenizer.decode(output[0])
                    predicted_sentences.append(predicted_sentence)
                
                p, r, f1 = batch_multi_pre_rec_f1(predicted_sentences, self.dev_source_sentences, self.dev_gold_edits, 
                                                  self.max_unchanged_words, self.beta, self.ignore_whitespace_casing, self.verbose, self.very_verbose)
                print("Precision   : %.4f" % p)
                print("Recall      : %.4f" % r)
                print("F_%.1f       : %.4f" % (self.beta, f1))
            except:
                print("No predictions...")

callbacks = [
    Evaluation(tokenizer=tokenizer, nth=config['evaluation_every_nth'],
               max_unchanged_words=max_unchanged_words, beta=beta, ignore_whitespace_casing=ignore_whitespace_casing,
               verbose=verbose, very_verbose=very_verbose, dev_input_sentences=dev_input_sentences, dev_source_sentences=dev_source_sentences,
               dev_gold_edits=dev_gold_edits),
    tf.keras.callbacks.TensorBoard(log_dir=config['log_file'], profile_batch=config['profile_batch']),
    tf.keras.callbacks.ModelCheckpoint(filepath=config['model_checkpoint_path'], save_weights_only=True, save_freq='epoch')
]

# %% [markdown]
# ---

# %%
if STEPS_PER_EPOCH:
    model.fit(dataset, callbacks=callbacks, epochs=EPOCHS, steps_per_epoch=STEPS_PER_EPOCH)
else:
    model.fit(dataset, callbacks=callbacks, epochs=EPOCHS)

# %% [markdown]
# ---

# %%
# checkpoint_filepath = './tmp/checkpoint/' # must be folder (/ at the end)

# model.load_weights(checkpoint_filepath)

# %% [markdown]
# ---


