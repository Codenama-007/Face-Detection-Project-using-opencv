import tensorflow as tf
import os
from tensorflow.keras import layers
from tensorflow.keras.models import Sequential


DATASET_PATH = "ExamCheatingDataset"

BATCH_SIZE = 32
IMG_HEIGHT = 180 
IMG_WIDTH = 180

training_set = tf.keras.utils.image_dataset_from_directory(
    f"{DATASET_PATH}/train" ,
    validation_split = 0.2 ,
    subset = "training" ,
    seed = 42 ,
    image_size = (IMG_WIDTH , IMG_HEIGHT) ,
    batch_size = BATCH_SIZE 
)

testing_set = tf.keras.utils.image_dataset_from_directory(
    f"{DATASET_PATH}/test" ,
    validation_split = 0.2 ,
    subset = 'validation' ,
    seed = 42 ,
    image_size = (IMG_WIDTH , IMG_HEIGHT)
)

normalization_layer = tf.keras.layers.Rescaling(
    1./255
)

data_augmentation = tf.keras.Sequential([
    layers.RandomFlip("horizontal" , input_shape = (IMG_WIDTH , IMG_HEIGHT , 3)) ,
    layers.RandomRotation(0.1) ,
    layers.RandomZoom(0.1)
])

class_names = training_set.class_names


model = Sequential(
    [
        data_augmentation ,
        layers.Rescaling(1./255) ,
        layers.Conv2D(16 , 3 , padding = 'same' , activation = 'relu') ,
        layers.MaxPooling2D() ,
        layers.Conv2D(32 , 3 , padding = 'same' , activation = 'relu') ,
        layers.MaxPooling2D() ,
        layers.Conv2D(64 , 3 , padding = 'same' , activation = 'relu') ,
        layers.MaxPooling2D() ,
        layers.Dropout(0.2) ,
        layers.Flatten() ,
        layers.Dense(128 , activation = 'relu') ,
        layers.Dense(len(class_names) , name = "outputs")
    ]
)

model.compile(
    optimizer = 'adam' ,
    loss = tf.keras.losses.SparseCategoricalCrossentropy(from_logits = True) ,
    metrics = ['accuracy']
)

model.summary()

epochs = 15 
history = model.fit(
    training_set ,
    validation_data = testing_set ,
    epochs = epochs
)
# print(class_names)

converter = tf.lite.TFLiteConverter.from_keras_model(model)
tflite_model = converter.convert()


print(" Saving the model ")
# save the model
with open("model.tflite" , "wb") as f:
    f.write(tflite_model)
print(" Model Successfully saved ")
