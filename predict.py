import tensorflow as tf
import numpy as np
from PIL import Image


MODEL_PATH = "model.tflite"

IMAGE_SIZE = (180, 180)

CLASS_NAMES = [
    "cheating",
    "giving_code",
    "giving_object",
    "looking_normal",
    "normal_act"
]


# --------------------------------------------------
# LOAD TFLITE MODEL
# --------------------------------------------------

interpreter = tf.lite.Interpreter(
    model_path=MODEL_PATH
)

interpreter.allocate_tensors()


# --------------------------------------------------
# GET MODEL INPUT / OUTPUT INFORMATION
# --------------------------------------------------

input_details = interpreter.get_input_details()
output_details = interpreter.get_output_details()

print("INPUT DETAILS")
print(input_details)

print("\nOUTPUT DETAILS")
print(output_details)


# --------------------------------------------------
# LOAD IMAGE
# --------------------------------------------------

image_path = "image_2.jpg"

image = Image.open(image_path).convert("RGB")

image = image.resize(IMAGE_SIZE)

image = np.array(image)

# Add batch dimension
image = np.expand_dims(image, axis=0)

# Convert to float32
image = image.astype(np.float32)

# Normalize
image = image / 255.0


# --------------------------------------------------
# RUN MODEL
# --------------------------------------------------

interpreter.set_tensor(
    input_details[0]["index"],
    image
)

interpreter.invoke()


# --------------------------------------------------
# GET OUTPUT
# --------------------------------------------------

output = interpreter.get_tensor(
    output_details[0]["index"]
)

print("\nRAW MODEL OUTPUT:")
print(output)


# --------------------------------------------------
# CONVERT LOGITS TO PROBABILITIES
# --------------------------------------------------

probabilities = tf.nn.softmax(output[0]).numpy()

predicted_index = np.argmax(probabilities)

predicted_class = CLASS_NAMES[predicted_index]

confidence = probabilities[predicted_index]


# --------------------------------------------------
# RESULT
# --------------------------------------------------

print("\n==============================")
print("PREDICTION")
print("==============================")

print("Class:", predicted_class)
print("Confidence:", f"{confidence * 100:.2f}%")