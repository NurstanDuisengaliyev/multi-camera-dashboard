# Step 2 benchmark

Machine: Darwin arm64, Python 3.9.6. Run length: 60 s, 6 cameras (incl. 2 test cameras).

**Average CPU use of the whole program: 0.86 cores**

| Camera | FPS | Motion ms | Quality ms | People (YOLO) ms |
|---|---|---|---|---|
| cam1 | 11 | 3.2 | 0.9 | 23.1 |
| cam2 | 12 | 2.0 | 0.8 | 21.3 |
| cam3 | 59 | 2.4 | 0.6 | 18.9 |
| cam4 | 13 | 2.4 | 0.8 | 18.3 |
| tamper | 12 | 3.2 | 0.8 | 18.0 |
| broken | 0 | 0.0 | 0.0 | 0.0 |

Events by type: {'motion': 10, 'camera_blurry': 2, 'camera_dark': 2, 'crowd': 4, 'camera_offline': 1}
