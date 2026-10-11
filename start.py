import time
import numpy as np
import datetime


for i in range(10):
    if (datetime.datetime.now().hour >= 18):
        break

   
    with open("cr.py")as file:
        exec(file.read())

    delay = max(45*60, np.random.randn() * 500 + 3600)
    print(f'sleeping {delay/3600:.2f}')
    time.sleep(delay)

