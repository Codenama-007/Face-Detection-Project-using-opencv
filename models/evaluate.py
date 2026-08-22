import numpy as np
import tensorflow as tf

from sklearn.metrics import (
    classification_report,
    confusion_matrix
)

MODEL_PATH = "models/best_model.keras"
DATASET_PATH = "ExamCheatingDataset/train"

IMG_SIZE = (224, 224)
BATCH_SIZE = 32
SEED = 42


# ==============================
# Load model
# ==============================

model = tf.keras.models.load_model(
    MODEL_PATH
)


# ==============================
# Load validation dataset
# ==============================

val_ds = tf.keras.utils.image_dataset_from_directory(
    DATASET_PATH,
    validation_split=0.2,
    subset="validation",
    seed=SEED,
    image_size=IMG_SIZE,
    batch_size=BATCH_SIZE,
    shuffle=False
)


class_names = val_ds.class_names

print("\nClasses:")
print(class_names)


# ==============================
# Collect predictions
# ==============================

y_true = []
y_pred = []


for images, labels in val_ds:

    predictions = model.predict(
        images,
        verbose=0
    )

    predicted_classes = np.argmax(
        predictions,
        axis=1
    )

    y_true.extend(
        labels.numpy()
    )

    y_pred.extend(
        predicted_classes
    )


y_true = np.array(y_true)
y_pred = np.array(y_pred)


# ==============================
# Classification report
# ==============================

print("\n==============================")
print("CLASSIFICATION REPORT")
print("==============================\n")

print(
    classification_report(
        y_true,
        y_pred,
        labels = [0 , 1 , 2 , 3 , 4] ,
        target_names=class_names,
        zero_division=0
    )
)


# ==============================
# Confusion Matrix
# ==============================

print("\n==============================")
print("CONFUSION MATRIX")
print("==============================\n")

cm = confusion_matrix(
    y_true,
    y_pred
)

print(cm)