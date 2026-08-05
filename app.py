import cv2 
import os
import time 


# given below code works for detecting Faces in the images that are stored in the particular folder 

# loading the Detector 
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')


print(os.getcwd())
folder = 'images'

if not os.path.isdir(folder):
    print(f"Folder '{folder}' not found. Create it and add some images first.")
    raise SystemExit(1)

print(os.listdir(folder))


for image in os.listdir(folder):
    # Accessing the Images from their Respective Path
    img = cv2.imread(f"{folder}/{image}")

    # cv2.imread returns None for non-image files (e.g. .txt, .DS_Store)
    if img is None:
        print(f"Skipping {image}: not a readable image")
        continue

    # Formula for resizing the images
    height , width = img.shape[:2]
    new_width = 800
    new_height = int(height * new_width / width)
    img = cv2.resize(img , (new_width , new_height))

    # Converting the image to Gray Scale and loading the face detector Model

    gray_Scale = cv2.cvtColor(img , cv2.COLOR_BGR2GRAY)
    faces_obtained = face_cascade.detectMultiScale(gray_Scale)

    if len(faces_obtained) == 0:
        print(f"No face found in {image}")
    else:
        print(f"Total Faces found in the {image} is {len(faces_obtained)}")
        for x , y , width , height in faces_obtained:
            # Displaying the Rectangle over the colored Image
            cv2.rectangle(img , (x , y) , (x + width , y + height) , (0 , 0 , 255) , 3)

    cv2.imshow("Images" , img)

    # Printing the size of the images
    print(f"{image} -> {img.shape}")


    # Type of delay basically
    cv2.waitKey(2000)

cv2.destroyAllWindows()