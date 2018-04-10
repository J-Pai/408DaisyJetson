import numpy as np
import cv2
import face_recognition
import sys
from multiprocessing import Queue
from pylibfreenect2 import Freenect2, SyncMultiFrameListener
from pylibfreenect2 import FrameType, Registration, Frame
from pylibfreenect2 import setGlobalLogger

setGlobalLogger(None)
try:
    print("OpenGL Pipeline")
    from pylibfreenect2 import OpenGLPacketPipeline
    pipeline = OpenGLPacketPipeline()
    print("HELLO WORLD")
except:
    print("CPU Pipeline")
    from pylibfreenect2 import CpuPacketPipeline
    pipeline = CpuPacketPipeline()

EXPOSURE_1 = 0
EXPOSURE_2 = 0

RGB_W = 1920
RGB_H = 1080

FACE_W = RGB_W
FACE_H = RGB_H
DEFAULT_FACE_TARGET_BOX = (int(RGB_W/2) - 125, int(RGB_H/2) - 125,
        int(RGB_W/2) + 125, int(RGB_H/2) + 125)

TRACK_W = RGB_W
TRACK_H = RGB_H
DEFAULT_TRACK_TARGET_BOX = (int(TRACK_W/2) - 600, int(TRACK_H/2) - 400,
        int(TRACK_W/2) + 600, int(TRACK_H/2) + 400)

FACE_COUNT = 0

CORRECTION_THRESHOLD = 0.50

class DaisyEye:
    cam = None
    known_faces = {}
    data_queue = None
    flipped = False

    def __init__(self, faces, data_queue = None, cam_num = 1,
            res = (FACE_W, FACE_H), flipped = False):
        if cam_num != -1:
            self.cam = cv2.VideoCapture(cam_num);
            if not self.cam.isOpened():
                print("Could not open camera...")
                sys.exit()

        for person in faces:
            image = face_recognition.load_image_file(faces[person])
            print(person)
            face_encoding_list = face_recognition.face_encodings(image)
            if len(face_encoding_list) > 0:
                self.known_faces[person] = face_encoding_list[0]
            else:
                print("\tCould not find face for person...")

        if cam_num != -1:
            self.cam.set(3, res[0])
            self.cam.set(4, res[1])
            self.cam.set(14, EXPOSURE_2)

        self.data_queue = data_queue
        self.flipped = flipped

    def __draw_bbox(self, valid, frame, bbox, color, text):
        if not valid:
            return
        cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), color, 2, 1)
        cv2.putText(frame, text, (bbox[0], bbox[1] - 4), \
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    def __scale_frame(self, frame, scale_factor = 1):
        if scale_factor == 1:
            return frame
        return cv2.resize(frame, (0,0), fx=scale_factor, fy=scale_factor)

    def __crop_frame(self, frame, crop_box):
        return frame[crop_box[1]:crop_box[3], crop_box[0]:crop_box[2],:].copy()

    def __init_tracker(self, frame, bbox, tracker_type = "BOOSTING"):
        tracker = None;

        print("Init Tracker with:", bbox, tracker_type)

        if tracker_type == "BOOSTING":
            tracker = cv2.TrackerBoosting_create()
        if tracker_type == "MIL":
            tracker = cv2.TrackerMIL_create()
        if tracker_type == "KCF":
            tracker = cv2.TrackerKCF_create()
        if tracker_type == "TLD":
            tracker = cv2.TrackerTLD_create()
        if tracker_type == "MEDIANFLOW":
            tracker = cv2.TrackerMedianFlow_create()
        if tracker_type == "GOTURN":
            tracker = cv2.TrackerGOTURN_create()
        if tracker_type == "MOSSE":
            tracker = cv2.TrackerMOSSE_create()
        if tracker_type == "CSRT":
            tracker = cv2.TrackerCSRT_create()
        if tracker_type == "DLIB":
            tracker = dlib.correlation_tracker()
            tracker.start_track(frame, \
                    dlib.rectangle(bbox[0], bbox[1], bbox[0] + bbox[2], bbox[1] + bbox[3]))
            return tracker

        ret = tracker.init(frame, bbox)

        if not ret:
            return None
        return tracker

    def view(self, face_bbox = DEFAULT_FACE_TARGET_BOX, bbox_list = []):
        print("Press q when image is ready")
        while True:
            _, frame = self.cam.read()
            if self.flipped:
                frame = cv2.flip(frame, 0)
            output_frame = frame.copy()

            self.__draw_bbox(ret, output_frame, face_bbox, (255,0,0), "Target")
            count = 0
            for bbox in bbox_list:
                self.__draw_bbox(ret, output_frame, bbox, (255, 0, 0), str(count))
                print(count, self.__bbox_overlap(face_bbox, bbox), self.__bbox_overlap(bbox, face_bbox))
                count += 1
            cv2.imshow("Eye View", output_frame)

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        cv2.destroyAllWindows()
        return ret, frame

    def __select_ROI(self, frame):
        bbox = cv2.selectROI(frame, False)
        cv2.destroyAllWindows()
        return bbox;

    def release_cam(self):
        cv2.destroyAllWindows()
        self.cam.release()

    """
    Scale from res1 to res2
    """
    def __scale_bbox(self, bbox, scale_factor = 1):
        scaled = (int(bbox[0] * scale_factor), \
                int(bbox[1] * scale_factor), \
                int(bbox[2] * scale_factor), \
                int(bbox[3] * scale_factor))
        return scaled

    """
    Standard bbox layout (left, top, right, bottom)
    bbox1 overlaps with bbox2?
    """
    def __bbox_overlap(self, bbox1, bbox2):
        if not bbox1 or not bbox2:
            return 0

        left = max(bbox1[0], bbox2[0])
        top = max(bbox1[1], bbox2[1])
        right = min(bbox1[2], bbox2[2])
        bottom = min(bbox1[3], bbox2[3])

        if left < right and top < bottom:
            return self.__bbox_area((left, top, right, bottom))
        return 0

    def __bbox_area(self, bbox):
        if not bbox:
            return 0
        (left, top, right, bottom) = bbox
        return (right - left) * (bottom - top)

    def __clean_color(self, color, bigdepth, min_range, max_range):
        color[(bigdepth < min_range) | (bigdepth > max_range)] = 0

    TOP_DIST_THRES = 200
    BOT_DIST_THRES = -200

    def __body_bbox(self, bigdepth, mid_width, mid_height, res):

        mid_dist = bigdepth[mid_height][mid_width]
        top_thres = mid_dist + self.TOP_DIST_THRES
        bot_thres = mid_dist + self.BOT_DIST_THRES

        left = 0
        top = 0
        right = mid_width + 100
        bottom = 0

        for h in range(mid_height, 0, -1):
            if bigdepth[h][mid_width] > top_thres:
                top = h
                break
        for h in range(mid_height, res[1]):
            if bigdepth[h][mid_width] < bot_thres:
                bottom = h
                break
        bot_mid_dist = bigdepth[bottom][mid_width]
        bot_thres = bot_mid_dist + self.TOP_DIST_THRES
        for w in range(mid_width, 0, -1):
            if bigdepth[bottom][w] > bot_thres:
                left = w
                break
        for w in range(mid_width, res[0]):
            if bigdepth[bottom][w] > bot_thres:
                right = w
                break


        return (left, top, right, bottom)

    def find_and_track_kinect(self, name, tracker = "CSRT",
            min_range = 0, max_range = 2000,
            track_target_box = DEFAULT_TRACK_TARGET_BOX,
            face_target_box = DEFAULT_FACE_TARGET_BOX,
            res = (RGB_W, RGB_H),
            video_out = True, debug = True):

        print("Starting Tracking")

        fn = Freenect2()
        num_devices = fn.enumerateDevices()

        if num_devices == 0:
            print("No device connected!")

        serial = fn.getDeviceSerialNumber(0)
        device = fn.openDevice(serial, pipeline = pipeline)

        listener = SyncMultiFrameListener(FrameType.Color | FrameType.Depth)

        device.setColorFrameListener(listener)
        device.setIrAndDepthFrameListener(listener)

        device.start()

        registration = Registration(device.getIrCameraParams(),
                device.getColorCameraParams())

        undistorted = Frame(512, 424, 4)
        registered = Frame(512, 424, 4)
        bigdepth = Frame(1920, 1082, 4)


        trackerObj = None
        face_count = 5
        face_process_frame = True

        bbox = None
        track_bbox = None

        head_h = 0
        body_left_w = 0
        body_right_w = 0
        center_w = 0

        while True:
            timer = cv2.getTickCount()

            frames = listener.waitForNewFrame()

            color = frames["color"]
            depth = frames["depth"]

            registration.apply(color, depth, undistorted, registered, bigdepth=bigdepth)

            bd = np.resize(bigdepth.asarray(np.float32), (1080, 1920))
            c = cv2.cvtColor(color.asarray(), cv2.COLOR_RGB2BGR)

            #self.__clean_color(c, bd, min_range, max_range)

            person_found = False
            face_bbox = None
            new_track_bbox = None

            if face_process_frame:
                small_c = self.__crop_frame(c, face_target_box)
                face_locations = face_recognition.face_locations(small_c, model="cnn")
                face_encodings = face_recognition.face_encodings(small_c, face_locations)
                for face_encoding in face_encodings:
                    matches = face_recognition.compare_faces(
                            [self.known_faces[name]], face_encoding, 0.6)
                    if len(matches) > 0 and matches[0]:
                        person_found = True
                        face_count += 1
                        (top, right, bottom, left) = face_locations[0]

                        left += face_target_box[0]
                        top += face_target_box[1]
                        right += face_target_box[0]
                        bottom += face_target_box[1]

                        face_bbox = (left, top, right, bottom)
                        mid_w = int((left + right) / 2)
                        mid_h = int((top + bottom) / 2)
                        new_track_bbox = self.__body_bbox(bd, mid_w, mid_h, res)

                        person_found = True

                        break
            face_process_frame = not face_process_frame

            overlap_pct = 0
            track_area = self.__bbox_area(track_bbox)
            # if track_area > 0 and face_bbox:
            #    overlap_area = self.__bbox_overlap(face_bbox, track_bbox)
            #    overlap_pct = min(overlap_area / self.__bbox_area(face_bbox),
            #            overlap_area / self.__bbox_area(track_bbox))
            if track_area > 0 and new_track_bbox:
                overlap_area = self.__bbox_overlap(new_track_bbox, track_bbox)
                overlap_pct = min(overlap_area / self.__bbox_area(new_track_bbox),
                        overlap_area / self.__bbox_area(track_bbox))

            # small_c = self.__crop_frame(c, track_target_box)
            small_c = self.__scale_frame(c, 0.5)

            if person_found and face_count >= FACE_COUNT and overlap_pct < CORRECTION_THRESHOLD:
                # bbox = (face_bbox[0] - track_target_box[0],
                #        face_bbox[1] - track_target_box[1],
                #        face_bbox[2] - face_bbox[0],
                #        face_bbox[3] - face_bbox[1])
                bbox = (new_track_bbox[0],
                        new_track_bbox[1],
                        new_track_bbox[2] - new_track_bbox[0],
                        new_track_bbox[3] - new_track_bbox[1])
                bbox = self.__scale_bbox(bbox, 0.5)
                trackerObj = self.__init_tracker(small_c, bbox, tracker)
                face_count = 0

            status = False

            if trackerObj is not None:
                status, trackerBBox = trackerObj.update(small_c)
                bbox = (int(trackerBBox[0]),
                        int(trackerBBox[1]),
                        int(trackerBBox[0] + trackerBBox[2]),
                        int(trackerBBox[1] + trackerBBox[3]))

            if bbox is not None:
                #track_bbox = (bbox[0] + track_target_box[0],
                #        bbox[1] + track_target_box[1],
                #        bbox[2] + track_target_box[0],
                #        bbox[3] + track_target_box[1])
                track_bbox = (bbox[0], bbox[1], bbox[2], bbox[3])
                track_bbox = self.__scale_bbox(bbox, 2)


            fps = cv2.getTickFrequency() / (cv2.getTickCount() - timer)

            w = 0
            h = 0

            if status:
                w = track_bbox[0] + int((track_bbox[2] - track_bbox[0])/2)
                h = track_bbox[1] + int((track_bbox[3] - track_bbox[1])/2)

                if (w < res[0] and w >= 0 and h < res[1] and h >= 0):
                    distanceAtCenter =  bd[h][w]
                    center = (w, h)
                    self.__update_individual_position("NONE", track_bbox, center, distanceAtCenter, res)

            if video_out:
                cv2.line(c, (w, 0), (w, res[1]), (0,255,0), 1)
                cv2.line(c, (0, h), (res[0], h), (0,255,0), 1)
                cv2.line(c, (0, head_h), (res[0], head_h), (0,0,0), 1)
                cv2.line(c, (body_left_w, 0), (body_left_w, res[1]), (0,0,255), 1)
                cv2.line(c, (body_right_w, 0), (body_right_w, res[1]), (0,0,255), 1)
                cv2.line(c, (center_w, 0), (center_w, res[1]), (0,0,255), 1)

                self.__draw_bbox(True, c, face_target_box, (255, 0, 0), "FACE_TARGET")
                self.__draw_bbox(True, c, track_target_box, (255, 0, 0), "TRACK_TARGET")
                self.__draw_bbox(status, c, track_bbox, (0, 255, 0), tracker)
                self.__draw_bbox(person_found, c, face_bbox, (0, 0, 255), name)
                self.__draw_bbox(person_found, c, new_track_bbox, (255, 0, 0), "BODY")

                c = self.__scale_frame(c, scale_factor = 0.5)

                cv2.putText(c, "FPS : " + str(int(fps)), (100,50),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,0,255), 1)
                if not status:
                    failedTrackers = "FAILED: "
                    failedTrackers += tracker + " "
                    cv2.putText(c, failedTrackers, (100, 80),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,142), 1)

                cv2.imshow("color", c)

            listener.release(frames)

            key = cv2.waitKey(1) & 0xff
            if key == ord('q'):
                self.__update_individual_position("STOP", track_bbox, distanceAtCenter, res)
                break

        cv2.destroyAllWindows()
        device.stop()
        device.close()

    def __update_individual_position(self, str_pos, track_bbox, center, distance, res):
        if self.data_queue is not None and self.data_queue.empty():
            self.data_queue.put((str_pos, track_bbox, center, distance, res))
