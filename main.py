import cv2
import mediapipe as mp
import time 


look_start_time = None
current_direction = 'CENTER'

risk = 0


face_detection = mp.solutions.face_detection.FaceDetection(
    model_selection=0,
    min_detection_confidence=0.5
)

capture = cv2.VideoCapture(0)

while True:
    ret , frame = capture.read()
    if not ret:
        print(" No frame Found ")
        break
    
    rgb_frame = cv2.cvtColor(frame , cv2.COLOR_BGR2RGB)
    
    results = face_detection.process(rgb_frame)
    current_time = time.time()
    
    direction = 'CENTER'
    
    if results.detections:
        status = 'MIL GAYA'
        # risk += 20
        detection = results.detections[0]
        bbox = detection.location_data.relative_bounding_box
        h , w , _ = frame.shape
        face_x = bbox.xmin + bbox.width / 2 
        face_y = bbox.ymin + bbox.height / 2 
        if face_x < 0.4:
            direction = 'LEFT'
                
        elif face_x > 0.6:
            direction = 'RIGHT'
            
        elif face_y > 0.6 :
            direction = 'BOTTOM'
            
        elif face_y < 0.4:
            direction = 'TOP'
              
        else:
            direction = 'CENTER'
                
        if direction != current_direction:
            current_direction = direction
            look_start_time = current_time 
        else:
            duration = current_time - look_start_time if look_start_time else 0
            
            if current_direction in ['LEFT' , 'RIGHT'] and duration >= 3:
                risk += 15
                look_start_time = current_time
                
        cv2.putText(frame , f"Direction : {direction}" , (50 , 150) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (255 , 255 , 0) , 2)
        cv2.putText(frame , f"Risk : {risk}" , (50 , 190) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (0 , 0 , 255) , 2)
        # for detection in results.detections:
            # bboxC = detection.location_data.relative_bounding_box
            
            # h , w , _ = frame.shape
            # face_x = bb
            # x = int(bboxC.xmin*w) 
            # y = int(bboxC.ymin*h)
            # width = int(bboxC.width*w)
            # height = int(bboxC.height*h)
            
            # # Drawing Rectangle around the face 
            
            # cv2.rectangle(frame , (x , y) , (x + width , y + height) , (0 , 0 , 255) , 2)        
            # cv2.putText(frame , status , (50 , 50) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (0 , 255 , 0) , 2)
            # cv2.putText(frame , f"{risk}" , (150 , 150) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (0 , 255 , 0) , 2)
            
            # if risk == 100:
            #     cv2.putText(frame , f"{risk}" , (150 , 150) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (255 , 0 , 0) , 2)
                
            
    else:
        status = 'NAHI MILA'
        cv2.putText(frame , f"{status}" , (50 , 50) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (255 , 0 , 0) , 2)
        
        
    cv2.putText(frame , status , (50 , 50) , cv2.FONT_HERSHEY_SIMPLEX , 1 , (0 , 255 , 0) , 2)
    
    
    if not ret:
        print(" No ret Found ")
        break
        
    cv2.imshow(" Exam Monitor " , frame)
    
    if cv2.waitKey(1) & 0xfff == ord('q'):
        break
    
capture.release()
cv2.destroyAllWindows()