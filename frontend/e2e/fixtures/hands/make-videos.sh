#!/bin/sh
# The fake-camera videos the hands E2E specs play (Chromium --use-file-for-fake-video-capture), made from MediaPipe's
# own test photos (Apache-2.0). 320x240, 10 fps, 3 s; Chromium loops them. Rerun only to change them: needs ffmpeg.
#   point.mjpeg            a pointing hand, fingertip at the centre of the picture, still
#   palm-sweep.mjpeg       an open hand sweeping across the picture
#   two-hands-apart.mjpeg  two open hands growing apart
#   two-flick.mjpeg        a two-finger hand sweeping across the picture
# Chromium plays them at its own ~30 fps, so each 3 s file loops about every second; the specs only need the motion's direction.
set -eu
cd "$(dirname "$0")"
tmp=$(mktemp -d)
for f in pointing_up right_hands victory; do curl -sfo "$tmp/$f.jpg" "https://storage.googleapis.com/mediapipe-assets/$f.jpg"; done
ffmpeg -loglevel error -y -i "$tmp/right_hands.jpg" -vf "crop=iw/2:ih:iw/2:0" "$tmp/open_palm.jpg"
bg="color=c=white:s=320x240:d=3:r=10"
ffmpeg -loglevel error -y -f lavfi -i "$bg" -loop 1 -i "$tmp/pointing_up.jpg" \
  -filter_complex "[1:v]scale=143:150[h];[0:v][h]overlay=x=92:y=90:shortest=1" -q:v 5 point.mjpeg
ffmpeg -loglevel error -y -f lavfi -i "$bg" -loop 1 -i "$tmp/open_palm.jpg" \
  -filter_complex "[1:v]scale=160:170[h];[0:v][h]overlay=x='10+t*46':y=35:shortest=1" -q:v 5 palm-sweep.mjpeg
ffmpeg -loglevel error -y -f lavfi -i "$bg" -loop 1 -i "$tmp/right_hands.jpg" \
  -filter_complex "[1:v]scale=w='trunc((176+t*42)/2)*2':h=-2:eval=frame[h];[0:v][h]overlay=x='(W-w)/2':y='(H-h)/2':shortest=1" \
  -q:v 5 two-hands-apart.mjpeg
ffmpeg -loglevel error -y -f lavfi -i "$bg" -loop 1 -i "$tmp/victory.jpg" \
  -filter_complex "[1:v]scale=-2:200[h];[0:v][h]overlay=x='10+t*50':y=20:shortest=1" -q:v 5 two-flick.mjpeg
rm -rf "$tmp"
