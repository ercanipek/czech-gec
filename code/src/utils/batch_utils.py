import tensorflow as tf
from transformers import TFAutoModelForSeq2SeqLM
from transformers import AutoTokenizer
from multiprocessing import Process
from tensorflow.python.client import device_lib 

LINE = "Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena \
Nebo nevím nějaké specifiské běloruské národní tradice, protože vyrostl jsem ve městě, kde oslává Vánoc neni tak rozšiřena"


def tokenize_line(line, tokenizer, max_length):
    def get_tokenized_sentences(line):
        line = line.decode('utf-8')
        tokenized = tokenizer(line, text_target=line, max_length=max_length, truncation=True, return_tensors="tf")
        return tokenized['input_ids'], tokenized['attention_mask'], tokenized['labels']
    input_ids, attention_mask, labels = tf.numpy_function(get_tokenized_sentences, inp=[line], Tout=[tf.int32, tf.int32, tf.int32])
    decoder_input_ids = tf.roll(labels, shift=1, axis=1)
    dato = {
        'input_ids': input_ids[0],
        'attention_mask': attention_mask[0],
        'decoder_input_ids': decoder_input_ids[0],
        'labels': labels[0]
    }
    return dato

def ensure_shapes(input_dict, max_length):
    return {key: tf.ensure_shape(val, (max_length)) for key, val in input_dict.items()}

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
    
def try_batch_size(model, tokenizer, lines, batch_size, max_length, lr=0.00001) -> bool:
    print(device_lib.list_local_devices())

    dataset = tf.data.Dataset.from_tensor_slices((lines))
    dataset = dataset.map(lambda line: tokenize_line(line, tokenizer, max_length), num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.map(lambda input_dict: ensure_shapes(input_dict, max_length), num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.map(split_features_and_labels, num_parallel_calls=tf.data.experimental.AUTOTUNE)
    dataset = dataset.batch(batch_size)
    dataset = dataset.prefetch(tf.data.experimental.AUTOTUNE)

    optimizer = tf.keras.optimizers.Adam(learning_rate=lr)
    model.compile(optimizer=optimizer)
    model.fit(dataset, epochs=2, steps_per_epoch=2)


def get_batch_size(model, tokenizer, max_length) -> int:
    NUM_LINES = 128
    MAX_BATCH_SIZE = 2049
    STEP_BATCH = 16

    lines = [LINE] *  NUM_LINES

    for batch_size in range(STEP_BATCH, MAX_BATCH_SIZE, STEP_BATCH):
        try:
            process = Process(target=try_batch_size, args=(model, tokenizer, lines, batch_size, max_length,))
            process.start()
            process.join()
            process.close()
            print(f"Allowed batch size {batch_size} for max_length {max_length}.")
        except:
            return batch_size - STEP_BATCH
        
def all_batch_sizes(model, tokenizer):
    MAX_LENGTH = 16384
    STEP_LENGTH = 16

    batch_sizes = []
    for max_length in range(STEP_LENGTH, MAX_LENGTH, STEP_LENGTH):
        batch_size = get_batch_size(model, tokenizer, max_length)
        if batch_size == 0:
            break
        batch_sizes.append((max_length, batch_size))
    return batch_sizes
        
def main():
    tokenizer = AutoTokenizer.from_pretrained("google/mt5-small")
    model = TFAutoModelForSeq2SeqLM.from_pretrained("google/mt5-small")
    filename = "mt5-small-batches.txt"

    batch_sizes = all_batch_sizes(model, tokenizer)

    with open(filename, "w") as f:
        f.writelines(batch_sizes)

    print(batch_sizes)


if __name__ == "__main__":
    main()