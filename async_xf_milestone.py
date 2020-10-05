from __future__ import print_function
import websocket
import datetime
import hashlib
import base64
import hmac
import json
from urllib.parse import urlencode
from wsgiref.handlers import format_date_time
from datetime import datetime
from time import mktime
import asyncio
import websockets
import pyttsx3
import pyaudio
import time
import config

STATUS_FIRST_FRAME = 0  # 第一帧的标识
STATUS_CONTINUE_FRAME = 1  # 中间帧标识
STATUS_LAST_FRAME = 2  # 最后一帧的标识


class Ws_Param(object):
    # 初始化
    def __init__(self, APPID, APIKey, APISecret):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret

        # 公共参数(common)
        self.CommonArgs = {"app_id": self.APPID}
        # 业务参数(business)，更多个性化参数可在官网查看
        self.BusinessArgs = {"domain": "iat", "language": "zh_cn", "accent": "mandarin", "vinfo": 1, "vad_eos": 10000}

    # 生成url
    def create_url(self):
        url = 'wss://ws-api.xfyun.cn/v2/iat'
        # 生成RFC1123格式的时间戳
        now = datetime.now()
        date = format_date_time(mktime(now.timetuple()))

        # 拼接字符串
        signature_origin = "host: " + "ws-api.xfyun.cn" + "\n"
        signature_origin += "date: " + date + "\n"
        signature_origin += "GET " + "/v2/iat " + "HTTP/1.1"
        # 进行hmac-sha256进行加密
        signature_sha = hmac.new(self.APISecret.encode('utf-8'), signature_origin.encode('utf-8'),
                                 digestmod=hashlib.sha256).digest()
        signature_sha = base64.b64encode(signature_sha).decode(encoding='utf-8')

        authorization_origin = "api_key=\"%s\", algorithm=\"%s\", headers=\"%s\", signature=\"%s\"" % (
            self.APIKey, "hmac-sha256", "host date request-line", signature_sha)
        authorization = base64.b64encode(authorization_origin.encode('utf-8')).decode(encoding='utf-8')
        # 将请求的鉴权参数组合为字典
        v = {
            "authorization": authorization,
            "date": date,
            "host": "ws-api.xfyun.cn"
        }
        # 拼接鉴权参数，生成url
        url = url + '?' + urlencode(v)
        # print("date: ",date)
        # print("v: ",v)
        # 此处打印出建立连接时候的url,参考本demo的时候可取消上方打印的注释，比对相同参数时生成的url与自己代码生成的url是否一致
        # print('websocket url :', url)
        return url


def blocking(text):
    engine = pyttsx3.init()
    engine.say(text)
    engine.runAndWait()


async def txt_to_audio(queue):
    while True:
        try:
            text = await queue.get()
            print(f'> {text}')
        except asyncio.CancelledError:
            continue
        if not text:
            break

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, blocking, text)
        try:
            await asyncio.sleep(0)
        finally:
            await future

        queue.task_done()


async def count(queue):
    for _ in range(10):
        await asyncio.sleep(1)
        txt_msg = str(_)
        print(f'{time.ctime()}> {txt_msg}')
        queue.put_nowait(txt_msg)


async def msg_send(ws):
    CHUNK = 520
    FORMAT = pyaudio.paInt16
    CHANNELS = 1
    RATE = 16000
    p = pyaudio.PyAudio()

    stream = p.open(format=FORMAT,
                    channels=CHANNELS,
                    rate=RATE,
                    input=True,
                    frames_per_buffer=CHUNK)
    print('---------------start Recording---------------')

    intervel = 0.04  # 发送音频间隔(单位:s)
    status = STATUS_FIRST_FRAME  # 音频的状态信息，标识音频是第一帧，还是中间帧、最后一帧

    for i in range(0, int(RATE / CHUNK * 10)):
        buf = stream.read(CHUNK)
        # 文件结束
        # if not buf:
        if i == int(RATE / CHUNK * 10):
            status = STATUS_LAST_FRAME
        # 第一帧处理
        # 发送第一帧音频，带business 参数
        # appid 必须带上，只需第一帧发送
        if status == STATUS_FIRST_FRAME:
            d = {"common": wsParam.CommonArgs,
                 "business": wsParam.BusinessArgs,
                 "data": {"status": 0, "format": "audio/L16;rate=16000",
                          "audio": str(base64.b64encode(buf), 'utf-8'),
                          "encoding": "raw"}}
            d = json.dumps(d)
            await ws.send(d)
            # print('---------------Sending first---------------')
            status = STATUS_CONTINUE_FRAME
        # 中间帧处理
        elif status == STATUS_CONTINUE_FRAME:
            d = {"data": {"status": 1, "format": "audio/L16;rate=16000",
                          "audio": str(base64.b64encode(buf), 'utf-8'),
                          "encoding": "raw"}}
            await ws.send(json.dumps(d))
            # print('---------------Sending middle---------------')
        # 最后一帧处理
        elif status == STATUS_LAST_FRAME:
            d = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                          "audio": str(base64.b64encode(buf), 'utf-8'),
                          "encoding": "raw"}}
            await ws.send(json.dumps(d))
            # print('---------------Sending finished---------------')
            await asyncio.sleep(1)
            break
        # 模拟音频采样间隔
        await asyncio.sleep(intervel)
    stream.stop_stream()
    stream.close()
    p.terminate()


async def msg_rev(ws, queue):
    async for message in ws:
        try:
            code = json.loads(message)["code"]
            sid = json.loads(message)["sid"]
            if code != 0:
                errMsg = json.loads(message)["message"]
                print("sid:%s call error:%s code is:%s" % (sid, errMsg, code))

            else:
                data = json.loads(message)["data"]["result"]["ws"]
                # print(json.loads(message))
                result = ""
                for i in data:
                    for w in i["cw"]:
                        result += w["w"]
                # print("sid:%s call success!,data is:%s" % (sid, json.dumps(data, ensure_ascii=False)))
                print(f'翻译结果: {result}')
                queue.put_nowait(result)

        except Exception as e:
            print("receive msg,but parse exception:", e)


async def echo(queue):
    uri = wsUrl
    # uri = "wss://echo.websocket.org/"
    async with websockets.connect(uri) as ws:
        msg_rev_task = asyncio.create_task(
            msg_rev(ws, queue))
        msg_send_task = asyncio.create_task(
            msg_send(ws))
        done, pending = await asyncio.wait(
            [msg_send_task, msg_rev_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def main():
    queue = asyncio.Queue()
    txt_to_audio_task = asyncio.create_task(txt_to_audio(queue))
    count_task = asyncio.create_task(count(queue))
    echo_task = asyncio.create_task(echo(queue))
    await count_task
    await txt_to_audio_task
    await echo_task
    await queue.join()


if __name__ == '__main__':
    wsParam = Ws_Param(APPID=config.APPID, APIKey=config.APIKey, APISecret=config.APISecret)
    websocket.enableTrace(False)
    wsUrl = wsParam.create_url()
    asyncio.run(main())
