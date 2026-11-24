
import os,sys
R=os.environ["YROOT"]; sys.path.insert(0,R); os.chdir(R)
from core.infra import trouble
print("真实台账渲染出来是这样：")
print(trouble.render())
