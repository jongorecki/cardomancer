# capture_card_back_ref.py
# ---------------------------------------------------------------------------
# Utility: capture a card back reference image from the camera.
#
# Place a face-down card on the staging area, park the carriage,
# aim the camera, and press SPACE to capture. The detected card image
# is saved as card_back_reference.png for use by the back-detection system.
# ---------------------------------------------------------------------------

import os
import cv2
from config import CARD_BACK_REF_PATH
from detection import detect_card_on_staging

def main():
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Could not open camera.")
        return

    print("Place a face-down card on the staging area.")
    print("Aim the camera so the card is visible.")
    print("Press SPACE to capture, ESC to quit.")

    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
        cv2.imshow("Live View", frame)

        key = cv2.waitKey(1) & 0xFF
        if key == 32:  # SPACE
            card_img = detect_card_on_staging(frame, debug=True)
            if card_img is None:
                print("No card detected! Adjust camera and try again.")
                continue

            cv2.imshow("Detected Card Back", card_img)
            print(f"Card image shape: {card_img.shape}")
            print("Press 's' to save as reference, any other key to retry.")
            save_key = cv2.waitKey(0) & 0xFF
            if save_key == ord('s'):
                cv2.imwrite(CARD_BACK_REF_PATH, card_img)
                print(f"Saved card back reference to: {CARD_BACK_REF_PATH}")
                break
            else:
                print("Discarded. Try again.")
        elif key == 27:  # ESC
            print("Aborted.")
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
