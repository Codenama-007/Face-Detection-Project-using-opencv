# this code is basically for the analysis part 
import os 

DATASET_PATH = "ExamCheatingDataset/train"

classes = os.listdir(DATASET_PATH)

print("Classes:")
for cls in classes:
    class_path = os.path.join(DATASET_PATH, cls)

    if os.path.isdir(class_path):
        images = os.listdir(class_path)
        print(f"{cls}: {len(images)} images")
# print(os.listdir(DATASET_PATH))