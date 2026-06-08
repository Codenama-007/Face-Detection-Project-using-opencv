import cv2 
import os
import time 


# given below code works for detecting Faces in the images that are stored in the particular folder 

# loading the Detector 
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')


print(os.getcwd())
folder = 'images'
print(os.listdir(f"{folder}"))


for image in os.listdir(folder):
    # Accessing the Images from their Respective Path
    img = cv2.imread(f"{folder}/{image}")
    
    
    
    # Formula for resizing the images 
    height , width = img.shape[:2]
    new_width = 800
    new_height = int(height * new_width / width)
    img = cv2.resize(img , (new_width , new_height) , fx = 0.5 , fy = 0.5)
    
    # Converting the image to Gray Scale and loading the face detector Model 
    
    gray_Scale = cv2.cvtColor(img , cv2.COLOR_BGR2GRAY)
    faces_obtained = face_cascade.detectMultiScale(gray_Scale)
    for x , y , width , height in faces_obtained:
        if not (x , y , width , height):
            print("No face found ")
            continue
        else:
            print(" Congratulations face was found ")
            print(f"Total Faces found in the {image} is {len(faces_obtained)}")
            # Displaying the Rectangle over the colored Image 
            cv2.rectangle(img , (x , y) , (x + width , y + height) , (0 , 0 , 255) , 3)
            cv2.imshow("Images" , img )
    

    # Printing the size of the images
    print(f"{image} -> {img.shape}")
    
    
    # Type of delay basically 
    cv2.waitKey(2000)

cv2.destroyAllWindows()