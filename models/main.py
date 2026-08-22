# ============================================================
# Exam Cheating Detection - TensorFlow Training
# ============================================================
# this code will basically generate the model for what we want 

import os
import numpy as np
import tensorflow as tf
from sklearn.utils.class_weight import compute_class_weight


# Below part is all about loading the dataset and dividing it in train and test 

DATASET_PATH = "ExamCheatingDataset/train"

IMG_SIZE = (224, 224)
BATCH_SIZE = 32
SEED = 42

MODEL_DIR = "models"
MODEL_PATH = os.path.join(MODEL_DIR, "best_model.keras")

os.makedirs(MODEL_DIR, exist_ok=True)



train_ds = tf.keras.utils.image_dataset_from_directory(
    DATASET_PATH,
    validation_split=0.2,
    subset="training",
    seed=SEED,
    image_size=IMG_SIZE,
    batch_size=BATCH_SIZE,
    shuffle=True
)

val_ds = tf.keras.utils.image_dataset_from_directory(
    DATASET_PATH,
    validation_split=0.2,
    subset="validation",
    seed=SEED,
    image_size=IMG_SIZE,
    batch_size=BATCH_SIZE,
    shuffle=False
)

class_names = train_ds.class_names

print("\nClasses:")
for i, name in enumerate(class_names):
    print(f"{i}: {name}")

# below is the part that will be used for AutoTuning the data

AUTOTUNE = tf.data.AUTOTUNE

train_ds = train_ds.prefetch(AUTOTUNE)
val_ds = val_ds.prefetch(AUTOTUNE)

# we are using Data Augmentation 
# We are doing this cause want the model to learn from clean data
# As this data is downloaded from kaggle and they suggested us this 

data_augmentation = tf.keras.Sequential([
    tf.keras.layers.RandomFlip("horizontal"),
    tf.keras.layers.RandomRotation(0.1),
    tf.keras.layers.RandomZoom(0.1),
    tf.keras.layers.RandomContrast(0.1),
], name="data_augmentation")

# we are here calculating the class weights 

class_counts = {}

for class_id, class_name in enumerate(class_names):

    class_path = os.path.join(
        DATASET_PATH,
        class_name
    )

    count = len([
        file for file in os.listdir(class_path)
        if os.path.isfile(
            os.path.join(class_path, file)
        )
    ])

    class_counts[class_id] = count


print("\nClass Distribution:")

for class_id, count in class_counts.items():
    print(
        f"{class_id}: "
        f"{class_names[class_id]} -> "
        f"{count} images"
    )


# Create label array for class weight calculation

labels = []

for class_id, count in class_counts.items():
    labels.extend([class_id] * count)

labels = np.array(labels)


class_weights_array = compute_class_weight(
    class_weight="balanced",
    classes=np.unique(labels),
    y=labels
)

class_weights = {
    class_id: weight
    for class_id, weight
    in enumerate(class_weights_array)
}


print("\nClass Weights:")

for class_id, weight in class_weights.items():
    print(
        f"{class_names[class_id]}: "
        f"{weight:.4f}"
    )


# Using the MobileNetV2 model 
# This is basically a pretrained model 
# So we dont need to train it from scratch 
 
base_model = tf.keras.applications.MobileNetV2(
    input_shape=(224, 224, 3),
    include_top=False,
    weights="imagenet"
)

base_model.trainable = False


# below is the stuff for building out the model

inputs = tf.keras.Input(
    shape=(224, 224, 3)
)

x = data_augmentation(inputs)

x = tf.keras.applications.mobilenet_v2.preprocess_input(x)

x = base_model(
    x,
    training=False
)

x = tf.keras.layers.GlobalAveragePooling2D()(x)

x = tf.keras.layers.Dropout(0.3)(x)

outputs = tf.keras.layers.Dense(
    len(class_names),
    activation="softmax"
)(x)

model = tf.keras.Model(
    inputs,
    outputs
)

# Display Model

model.summary()

# Compile Model


model.compile(
    optimizer=tf.keras.optimizers.Adam(
        learning_rate=1e-3
    ),
    loss="sparse_categorical_crossentropy",
    metrics=["accuracy"]
)


# Callbacks

callbacks = [

    tf.keras.callbacks.EarlyStopping(
        monitor="val_loss",
        patience=3,
        restore_best_weights=True
    ),

    tf.keras.callbacks.ModelCheckpoint(
        MODEL_PATH,
        monitor="val_loss",
        save_best_only=True
    ),

    tf.keras.callbacks.ReduceLROnPlateau(
        monitor="val_loss",
        factor=0.2,
        patience=2,
        min_lr=1e-7
    )
]


# Stage 1 Training

print("\n===================================")
print("STAGE 1: Training Classifier")
print("===================================\n")

history = model.fit(
    train_ds,
    validation_data=val_ds,
    epochs=10,
    class_weight=class_weights,
    callbacks=callbacks
)


# Fine Tuning

print("\n===================================")
print("STAGE 2: Fine Tuning MobileNetV2")
print("===================================\n")


base_model.trainable = True


# Freeze earlier layers
for layer in base_model.layers[:-30]:
    layer.trainable = False


# Recompile with very small learning rate

model.compile(
    optimizer=tf.keras.optimizers.Adam(
        learning_rate=1e-5
    ),
    loss="sparse_categorical_crossentropy",
    metrics=["accuracy"]
)


fine_tune_callbacks = [

    tf.keras.callbacks.EarlyStopping(
        monitor="val_loss",
        patience=3,
        restore_best_weights=True
    ),

    tf.keras.callbacks.ModelCheckpoint(
        MODEL_PATH,
        monitor="val_loss",
        save_best_only=True
    ),

    tf.keras.callbacks.ReduceLROnPlateau(
        monitor="val_loss",
        factor=0.2,
        patience=2,
        min_lr=1e-7
    )
]


history_fine = model.fit(
    train_ds,
    validation_data=val_ds,
    epochs=10,
    class_weight=class_weights,
    callbacks=fine_tune_callbacks
)


# 13. Save Final Model

model.save(MODEL_PATH)

print("\n===================================")
print("TRAINING COMPLETE")
print("===================================")

print(f"Model saved to: {MODEL_PATH}")