import cv2
import time 
import mediapipe as mp


# loading the model
face_detection = mp.solutions.face_detection.FaceDetection(
    model_selection=0,
    min_detection_confidence=0.5
)
# current_time = time.time()
# previous_time = 0
capture = cv2.VideoCapture(0)

while True:
    success , frame = capture.read()
    if not success:
        print(" Sorry my man No frames were Found ")
        
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
            
            width = int(bbox.width * width)
            height = int(bbox.height * height)
            
            cv2.rectangle(frame , (x , y) , (x + width , y + height) , (0 , 255 , 0) , 2)
    
    fps = capture.get(cv2.CAP_PROP_FPS)
    
    cv2.putText(frame , f'{fps}' , (60 , 60) , cv2.FONT_HERSHEY_COMPLEX , 1.5 , (0 , 255 , 255) , 3)
    cv2.imshow("face detection",frame)
        
    if cv2.waitKey(1) & 0xff == ord('q'):
        break
    
capture.release()
cv2.destroyAllWindows()