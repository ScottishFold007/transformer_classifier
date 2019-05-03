import argparse
import logging
import sys

# file_handler = logging.FileHandler(filename="tmp.log")
stdout_handler = logging.StreamHandler(sys.stdout)
handlers = [stdout_handler]

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s - %(message)s",
    handlers=handlers,
)

logger = logging.getLogger()
logger.info("start")


import tensorflow_datasets as tfds
import tensorflow as tf

from utensor.optimizer import CustomSchedule, loss_function
from utensor.dataset import Dataset
from utensor.model import Transformer
from utensor.dataset import load_dataset
from utensor.masking import create_masks
import pickle
from sklearn.metrics import classification_report
import time
import os
import json

tf.keras.backend.clear_session()


def test_acc(batch=32, test_dataset=[], transformer=[], test_accuracy=[], test_loss=[]):
    for (batch, (inp, tar)) in enumerate(test_dataset):
        logger.debug("input: {}".format(inp.shape))
        logger.debug("target: {}".format(tar.shape))

        enc_padding_mask = create_masks(inp, tar)

        predictions, _, _ = transformer(inp, tar, False, enc_padding_mask, None, None)
        logger.debug("predictions: {}".format(predictions.shape))
        logger.debug("tar_real: {}".format(tar.shape))

        test_accuracy(tar, predictions)
        test_loss(loss_function(tar, predictions))


def train(args):

    params = dict(
        MAX_LENGTH=args.MAX_LENGTH,
        BUFFER_SIZE=args.BUFFER_SIZE,
        BATCH_SIZE=args.BATCH_SIZE,
        EPOCHS=args.EPOCHS,
        num_heads=args.num_heads,
        num_layers=args.num_layers,
        d_model=args.d_model,
        dff=args.dff,
        vocab_dim=args.vocab_dim,
        dropout_rate=args.dropout_rate,
        test_partition=args.test_partition,
        dataset_file=args.dataset_file,
        checkpoint_path=args.checkpoint_path,
        retrain=args.retrain,
    )

    # save parameters
    logger.info("saving parameters to {}".format(params["checkpoint_path"]))
    json.dump(params, open(params["checkpoint_path"] + "/params.json", "w"))

    # load the dataset
    logger.info("loading dataset")
    train_dataset, val_dataset, tokenizer_source, tokenizer_target = load_dataset(
        params=params
    )

    logger.debug(
        "FORMAT DATASET: the dataset consists of a query text and a target class (numerical value)"
    )

    example_entry = [i for i in train_dataset][0]
    logger.debug("example entry: {}".format(example_entry[0][0]))
    logger.debug("example class: {}".format(example_entry[1][0]))

    sample_string = [
        tokenizer_source.decode([i]) for i in example_entry[0].numpy()[0][1:3]
    ]
    sample_string = "".join(sample_string)

    tokenized_string = tokenizer_source.encode(sample_string)
    sample_class = tokenizer_target.decode_example(example_entry[1][0].numpy())
    tokenized_class = tokenizer_target.encode_example(sample_class)

    logger.debug("sample string: {}".format(sample_string))
    logger.debug("tokenized string: {}".format(tokenized_string))
    for ts in tokenized_string:
        logger.debug("{} ----> {}".format(ts, tokenizer_source.decode([ts])))
    logger.debug(
        "original class: {} ===> tokenized class: {}".format(
            sample_class, tokenized_class
        )
    )
    logger.debug("Number of classes: {}".format(tokenizer_target.num_classes))

    input_vocab_size = tokenizer_source.vocab_size + 2
    target_vocab_size = tokenizer_target.num_classes

    logger.info("setup learning rate and optimizer")
    # Setup the learning rate and optimizer
    learning_rate = CustomSchedule(params["d_model"])
    optimizer = tf.keras.optimizers.Adam(
        learning_rate, beta_1=0.9, beta_2=0.98, epsilon=1e-9
    )

    logger.info("setup loss function")
    # setup loss
    train_loss = tf.keras.metrics.Mean(name="train_loss")
    train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name="train_accuracy")

    test_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name="test_accuracy")
    test_loss = tf.keras.metrics.Mean(name="test_loss")

    logger.info("Setup transformer model")
    # setup Transformer Model
    transformer = Transformer(
        params["num_layers"],
        params["d_model"],
        params["num_heads"],
        params["dff"],
        input_vocab_size,
        target_vocab_size,
        params["dropout_rate"],
    )

    logger.info(
        "input_vocab_size: {} classes: {}".format(input_vocab_size, target_vocab_size)
    )

    # setup checkpoints
    logger.info("Setup Checkpoints: {}".format(params["checkpoint_path"]))
    ckpt = tf.train.Checkpoint(transformer=transformer, optimizer=optimizer)
    ckpt_manager = tf.train.CheckpointManager(
        ckpt, params["checkpoint_path"], max_to_keep=2
    )

    # if a checkpoint exists, restore the latest checkpoint.
    if ckpt_manager.latest_checkpoint and params["retrain"]:
        ckpt.restore(ckpt_manager.latest_checkpoint)
        logger.info("Latest checkpoint restored!!")
    else:
        logger.info("Initializing from scratch.")

    # define training function step
    # @tf.function
    def train_step(inp, tar):

        logger.debug("input: {}".format(inp.shape))
        logger.debug("target: {}".format(tar.shape))

        enc_padding_mask = create_masks(inp, tar)

        logger.debug("enc_padding_mask: {}".format(enc_padding_mask.shape))

        with tf.GradientTape() as tape:
            predictions, enc_output, _ = transformer(
                inp, tar, True, enc_padding_mask, None, None
            )

            logger.debug("predictions: {}".format(predictions.shape))
            logger.debug("tar_real: {}".format(tar.shape))
            logger.debug("enc_output: {}".format(enc_output.shape))
            # logger.debug("enc_output: {}".format(enc_output.shape))

            loss = loss_function(tar, predictions)

        gradients = tape.gradient(loss, transformer.trainable_variables)
        optimizer.apply_gradients(zip(gradients, transformer.trainable_variables))

        train_loss(loss)
        train_accuracy(tar, predictions)

    # training loop
    best_test_acc = 0
    for epoch in range(params["EPOCHS"]):
        start = time.time()

        train_loss.reset_states()
        train_accuracy.reset_states()

        # inp -> portuguese, tar -> english
        for (batch, (inp, tar)) in enumerate(train_dataset):
            train_step(inp, tar)
            if batch % 500 == 0:
                print(
                    "Epoch {} Batch {} Loss {:.4f} Accuracy {:.4f}".format(
                        epoch + 1, batch, train_loss.result(), train_accuracy.result()
                    )
                )

        print(
            "Epoch {} Train Loss {:.4f} Accuracy {:.4f}".format(
                epoch + 1, train_loss.result(), train_accuracy.result()
            )
        )

        # Perform accuracy over the test dataset
        test_accuracy.reset_states()
        test_loss.reset_states()
        test_acc(
            batch=32,
            test_dataset=val_dataset,
            transformer=transformer,
            test_accuracy=test_accuracy,
            test_loss=test_loss,
        )

        print(
            "Epoch {} Test Loss {:.4f} Accuracy {:.4f}".format(
                epoch + 1, test_loss.result(), test_accuracy.result()
            )
        )

        if best_test_acc < test_accuracy.result():
            ckpt_save_path = ckpt_manager.save()
            print(
                "Saving checkpoint for epoch {} at {}".format(epoch + 1, ckpt_save_path)
            )
            best_test_acc = test_accuracy.result()

        print("Time taken for 1 epoch: {} secs\n".format(time.time() - start))


def get_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--MAX_LENGTH", type=int, default=40)
    parser.add_argument("--BUFFER_SIZE", type=int, default=5000)
    parser.add_argument("--BATCH_SIZE", type=int, default=32)
    parser.add_argument("--EPOCHS", type=int, default=100)
    parser.add_argument("--num_heads", default=8, type=int)
    parser.add_argument("--num_layers", default=4, type=int)
    parser.add_argument("--d_model", default=64, type=int)
    parser.add_argument("--dff", default=264, type=int)
    parser.add_argument("--vocab_dim", default=10000, type=int)
    parser.add_argument("--dropout_rate", default=0.1, type=float)
    parser.add_argument("--test_partition", default=0.2, type=float)
    parser.add_argument("--dataset_file", type=str, required=True)
    parser.add_argument("--checkpoint_path", type=str, required=True)
    parser.add_argument("--retrain", default=False, action="store_true")

    return parser


if __name__ == "__main__":
    args = get_parser().parse_args()
    os.makedirs(args.checkpoint_path, exist_ok=True)
    train(args)
