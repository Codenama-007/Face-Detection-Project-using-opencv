import cv2
import time 
import mediapipe as mp


# loading the model
face_detection = mp.solutions.face_detection.FaceDetection(
    model_selection=0,
    min_detection_confidence=0.5
)
capture = cv2.VideoCapture(0)
previous_time = time.time()

while True:
    success , frame = capture.read()
    if not success:
        print(" Sorry my man No frames were Found ")
        break

    # Loading the face detection model over here
    # 1. Coverting the BGR frame to RGB

    rgb_frame = cv2.cvtColor(frame , cv2.COLOR_BGR2RGB)

    # 2. Detecting the Faces
    detected_faces = face_detection.process(rgb_frame)

    # 3. For Drawing Rectangles around detected Faces
    if detected_faces.detections:
        height , width , channels = frame.shape

        for detection in detected_faces.detections:
            bbox = detection.location_data.relative_bounding_box

            x = int(bbox.xmin * width)
            y = int(bbox.ymin * height)

            box_width = int(bbox.width * width)
            box_height = int(bbox.height * height)

            cv2.rectangle(frame , (x , y) , (x + box_width , y + box_height) , (0 , 255 , 0) , 2)

    # Actual real-time FPS measured from frame-to-frame timing
    current_time = time.time()
    fps = 1 / (current_time - previous_time) if current_time != previous_time else 0
    previous_time = current_time

    cv2.putText(frame , f'{int(fps)}' , (60 , 60) , cv2.FONT_HERSHEY_COMPLEX , 1.5 , (0 , 255 , 255) , 3)
    cv2.imshow("face detection",frame)
        
    if cv2.waitKey(1) & 0xff == ord('q'):
        break
    
capture.release()
cv2.destroyAllWindows()