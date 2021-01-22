import numpy as np
import random
from tensorflow.keras.preprocessing.image import load_img
from tensorflow.keras.preprocessing.text import Tokenizer
from tensorflow.keras.layers import (
    Input,
    Conv2D,
    Dense,
    Flatten,
    Embedding,
    Concatenate,
    GlobalMaxPool1D,
)
from tensorflow.keras.models import Model, load_model
import os
import json
import mlflow
import mlflow.tensorflow


tracking_uri = (
    "http://testuser:password@ec2-18-218-100-222.us-east-2.compute.amazonaws.com"
)
s3_bucket = "s3://docuedge-mlflow-bucket"  # replace this value


def read_data(path):
    bow = open(path, "r")
    data = bow.readlines()
    all_data_paths = []
    all_texts = []
    y_labels = {}
    for line in data:
        line_data = line.split("####")
        all_data_paths.append(line_data[0])
        all_texts.append(line_data[-1][:-1])
        label = line_data[0].split("/")[-2]
        if label not in y_labels:
            y_labels[label] = len(y_labels)

    rev_labels = {}
    for key, val in y_labels.items():
        rev_labels[val] = key

    return all_data_paths, y_labels, rev_labels, all_texts


def tokenize_sentence(sentence, tokenizer, maximum_word_length):
    updated_sentence = sentence.split(" ")
    tok_sent = []
    for word in updated_sentence:
        if word in tokenizer.word_index:
            tok_sent.append(tokenizer.word_index[word])
        else:
            tok_sent.append(0)
    if len(tok_sent) != maximum_word_length:
        delta = maximum_word_length - len(tok_sent)
        for i in range(delta):
            tok_sent.append(0)
    return tok_sent


def data_loader_text(
    bs, data, y_lab, tokenizer, text_data, image_input_shape, max_word_length
):
    while True:
        images = []
        labels = []
        texts = []
        while len(images) < bs:
            indice = random.randint(0, len(data) - 1)
            target = data[indice].split("/")[-2]
            labels.append(y_lab[target])

            test_img = np.asarray(load_img(data[indice], target_size=image_input_shape))
            img = np.divide(test_img, 255.0)
            images.append(img)

            tok_sen = tokenize_sentence(
                text_data[indice], tokenizer, maximum_word_length=max_word_length
            )
            texts.append(tok_sen)
        yield [np.asarray(images), np.asarray(texts)], np.asarray(labels)


def model_arc(y_labels, tokenizer, text_model_inp_shape, image_inp_shape):
    inp_layer_texts = Input(shape=text_model_inp_shape)
    inp_layer_images = Input(shape=image_inp_shape)

    embedding_layer = Embedding(
        input_dim=len(tokenizer.word_index) + 1,
        output_dim=64,
        input_length=text_model_inp_shape,
        trainable=True,
    )(inp_layer_texts)
    pooling_layer = GlobalMaxPool1D()(embedding_layer)
    dense_layer = Dense(units=64, activation="relu")(pooling_layer)
    # lstm_layer = Bidirectional(LSTM(units=32))(embedding_layer)

    conv_layer = Conv2D(filters=64, kernel_size=(2, 2), activation="relu")(
        inp_layer_images
    )
    flatten_layer = Flatten()(conv_layer)

    concat_layer = Concatenate()([flatten_layer, dense_layer])
    out_layer = Dense(len(y_labels), activation="softmax")(concat_layer)

    model = Model([inp_layer_images, inp_layer_texts], out_layer)
    model.compile(
        optimizer="adam", loss="sparse_categorical_crossentropy", metrics=["accuracy"]
    )
    return model


def train_hybrid_v1(
    text_plus_file_path: str,
    batch_size: int,
    epochs: int,
    image_shape: int,
    max_words: int,
    artifact_name: str,
    save_dir_path: str,
    trained_model_path: str,
    experiment_name: str,
):

    mlflow.set_tracking_uri(tracking_uri)
    client = mlflow.tracking.MlflowClient(tracking_uri=tracking_uri)
    try:
        expr_name = experiment_name  # create a new experiment (do not replace)
        mlflow.create_experiment(expr_name, s3_bucket)
        mlflow.set_experiment(expr_name)
        experiment = mlflow.get_experiment_by_name(experiment_name)
    except:
        experiment = mlflow.get_experiment_by_name(experiment_name)

    all_imgs_path, y_labels, rev_labels, all_text = read_data(path=text_plus_file_path)
    num_train_img = len(all_imgs_path)

    with open(
        os.path.join(save_dir_path, artifact_name, f"rev_labels_{artifact_name}.json"),
        "w+",
    ) as tar:
        json.dump(rev_labels, tar)

    print("target_encodings: ", y_labels)
    print("Number of training images: ", num_train_img)

    bow = open(text_plus_file_path, "r")
    tokenizer = Tokenizer()
    tokenizer.fit_on_texts(bow.read().split("####"))

    train_gen = data_loader_text(
        tokenizer=tokenizer,
        y_lab=y_labels,
        data=all_imgs_path,
        text_data=all_text,
        bs=batch_size,
        image_input_shape=(image_shape, image_shape, 3),
        max_word_length=max_words,
    )
    if os.path.isfile(trained_model_path):
        model = load_model(trained_model_path)
    else:
        model = model_arc(
            y_labels=y_labels,
            tokenizer=tokenizer,
            text_model_inp_shape=(max_words,),
            image_inp_shape=(image_shape, image_shape, 3),
        )
    mlflow.tensorflow.autolog(every_n_iter=1)
    with mlflow.start_run(experiment_id=experiment.experiment_id):
        mlflow.log_metrics(
            {
                "batch_size": batch_size,
                "epochs": epochs,
                "image_shape": image_shape,
                "max_words": max_words,
            }
        )
        history = model.fit(
            x=train_gen,
            steps_per_epoch=num_train_img // batch_size,
            epochs=epochs,
        )
        model.save(
            filepath=os.path.join(
                save_dir_path, artifact_name, "document_classifier.h5"
            )
        )
        meta_data_path = os.path.join(save_dir_path, artifact_name)
        for artifact in sorted(os.listdir(meta_data_path)):
            if artifact != ".DS_Store":
                artifact_path = os.path.join(meta_data_path, artifact)
                if (
                    os.path.isfile(artifact_path)
                    and artifact_path.split(".")[-1] != "h5"
                ):
                    print(f"artifact to be uploaded is: {artifact}")
                    mlflow.log_artifact(local_path=artifact_path)

        artifact_uri = mlflow.get_artifact_uri()
        print(artifact_uri)
        mlflow.end_run()
