import errno
import os
import signal
import functools

class TimeoutError(Exception):
    pass

def timeout(seconds=10, error_message=os.strerror(errno.ETIME)):
    def decorator(func):
        def _handle_timeout(signum, frame):
            raise TimeoutError(error_message)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(seconds)
            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return wrapper

    return decorator

import sys
sys.path.append('..')

import os
import time
import shutil
import tensorflow as tf

from transformers import TFAutoModelForSeq2SeqLM
from transformers import AutoTokenizer
from transformers import AutoConfig
import json

from m2scorer.levenshtein import batch_multi_pre_rec_f1_part, batch_multi_pre_rec_f1
from m2scorer.m2scorer import load_annotation

from tensorflow.keras import mixed_precision

from utils.udpipe_tokenizer.udpipe_tokenizer import UDPipeTokenizer

from multiprocessing import Process, Queue, Manager, Pool

def main():
    with open('config-eval.json') as json_file:
        config = json.load(json_file)
    
    SEED = config['seed']

    # data loading
    M2_DATA = config['m2_data']
    MAX_LENGTH = config['max_length']
    BATCH_SIZE = config['batch_size']
    
    # model
    MODEL = config['model']
    TOKENIZER = config['tokenizer']
    FROM_CONFIG = config['from_config']
    
    # logs
    MODEL_CHECKPOINT_PATH = config['model_checkpoint_path']

    # evaluation
    MAX_UNCHANGED_WORDS = config['max_unchanged_words']
    BETA = config['beta']
    IGNORE_WHITESPACE_CASING = config['ignore_whitespace_casing']
    VERBOSE = config['verbose']
    VERY_VERBOSE = config['very_verbose']
    
    tf.random.set_seed(SEED)
    
    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)
    
    
    # loading of dataset:
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
        
    def get_tokenized_sentences(line):
        line = line.decode('utf-8')
        tokenized = tokenizer(line, max_length=MAX_LENGTH, truncation=True, return_tensors="tf")
        return tokenized['input_ids'], tokenized['attention_mask']

    def create_error_line(line):
        input_ids, attention_mask = tf.numpy_function(get_tokenized_sentences, inp=[line], Tout=[tf.int32, tf.int32])
        dato = {
            'input_ids': input_ids[0],
            'attention_mask': attention_mask[0],
        }
        return dato

    dev_source_sentences, dev_gold_edits = load_annotation(M2_DATA)
        
    dataset =  tf.data.Dataset.from_tensor_slices((dev_source_sentences))
    dataset = dataset.map(create_error_line, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.map(split_features_and_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.padded_batch(BATCH_SIZE, padded_shapes={'input_ids': [None], 'attention_mask': [None]})
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)
    
    # policy = mixed_precision.Policy('mixed_float16')
    # mixed_precision.set_global_policy(policy)
    
    strategy = tf.distribute.MirroredStrategy()
    print('Number of devices: %d' % strategy.num_replicas_in_sync)

    with strategy.scope():
        if FROM_CONFIG:
            config = AutoConfig.from_pretrained(MODEL)
            model = TFAutoModelForSeq2SeqLM.from_config(config)
        else:
            model = TFAutoModelForSeq2SeqLM.from_pretrained(MODEL)

    # model.model.encoder.embed_scale = tf.cast(model.model.encoder.embed_scale, tf.float16)
    # model.model.decoder.embed_scale = tf.cast(model.model.decoder.embed_scale, tf.float16)

    udpipe_tokenizer = UDPipeTokenizer("cs")

    while True:
        if os.path.isdir(MODEL_CHECKPOINT_PATH):
            unevaluated = [f for f in os.listdir(MODEL_CHECKPOINT_PATH) if f.startswith('ckpt')]
            
            for unevaluated_checkpoint in unevaluated:
                try:
                    step = int(unevaluated_checkpoint[5:])
                    result_dir = os.path.join(MODEL_CHECKPOINT_PATH, "results")

                    model.load_weights(os.path.join(MODEL_CHECKPOINT_PATH, unevaluated_checkpoint + "/")).expect_partial()

                    print("Generating...")
                    predicted_sentences = []

                    for i, batch in enumerate(dataset):
                        print(f"Generate {i+1}. batch.") 
                        preds = model.generate(batch['input_ids'])
                        batch_sentences = tokenizer.batch_decode(preds, skip_special_tokens=True)
                        predicted_sentences = predicted_sentences + batch_sentences
                    
                    print("End of generating...")

                    print("Udpipe tokenization...")
                    tokenized_predicted_sentences = []

                    for i, line in enumerate(predicted_sentences):
                        if i % BATCH_SIZE == 0:
                            print(f"Tokenize {i+BATCH_SIZE} sentences.")
                        tokenization = udpipe_tokenizer.tokenize(line)
                        sentence = " ".join([token.string for token in tokenization[0]]) if len(tokenization) > 0 else ""
                        tokenized_predicted_sentences.append(sentence)

                    print("End of tokenization...")

                    print("Compute metrics...")
                    total_stat_correct, total_stat_proposed, total_stat_gold = 0, 0, 0 

                    @timeout(1)
                    def compute_m2_part(tokenized_predicted_sentences, dev_source_sentences, dev_gold_edits):
                        stat_correct, stat_proposed, stat_gold = batch_multi_pre_rec_f1_part(
                            tokenized_predicted_sentences,
                            dev_source_sentences,
                            dev_gold_edits,
                            MAX_UNCHANGED_WORDS, BETA, IGNORE_WHITESPACE_CASING, VERBOSE, VERY_VERBOSE)
                        return stat_correct, stat_proposed, stat_gold

                    size = 10
                    for i in range(0, len(tokenized_predicted_sentences), size):
                        try:
                            stat_correct, stat_proposed, stat_gold = compute_m2_part(tokenized_predicted_sentences[i:i+size], 
                                dev_source_sentences[i:i+size], 
                                dev_gold_edits[i:i+size])

                            total_stat_correct += stat_correct
                            total_stat_proposed += stat_proposed
                            total_stat_gold += stat_gold
                            
                            p  = total_stat_correct / total_stat_proposed if total_stat_proposed > 0 else 0
                            r  = total_stat_correct / total_stat_gold if total_stat_gold > 0 else 0
                            f1 = (1.0+BETA*BETA) * p * r / (BETA*BETA*p+r) if (p+r) > 0 else 0
                            print(f"Step {i+1}")
                            print("Precision:\t", p)
                            print("Recall:\t", r)
                            print("F1:\t", f1)
                        except:
                            print(f"Skip {size}...")

                        

                    print("End of computing...")

                    print("Write into files...")
                    p  = total_stat_correct / total_stat_proposed if total_stat_proposed > 0 else 0
                    r  = total_stat_correct / total_stat_gold if total_stat_gold > 0 else 0
                    f1 = (1.0+BETA*BETA) * p * r / (BETA*BETA*p+r) if (p+r) > 0 else 0
                    file_writer = tf.summary.create_file_writer(result_dir)
                    with file_writer.as_default():
                        tf.summary.scalar('epoch_precision', p, step)
                        tf.summary.scalar('epoch_recall', r, step)
                        tf.summary.scalar('epoch_f1', f1, step)

                        text = "\n".join(predicted_sentences[0:20])
                        print(text)
                        tf.summary.text("predictions", text, step)

                    print(f"Delete: {os.path.join(MODEL_CHECKPOINT_PATH, unevaluated_checkpoint)}")
                    shutil.rmtree(os.path.join(MODEL_CHECKPOINT_PATH, unevaluated_checkpoint))
                except:
                    print("Something went wrong... Try again...")

        time.sleep(10)

if __name__ == '__main__':
    main()

