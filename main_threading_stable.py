# 唤醒以后，重新按单句进行录制并转换文字。调试中发现有上下文语境以后语音的准确率反而降低。
# 分两个线程，线程1 - 查询PLC状态；线程2 - 发送语音，返回文字，当收到提示音是，进入下命令语句接收。
# 发声用的是同一个硬件设备，涉及硬件设备协调的问题。硬件发声会堵塞程序，所以单独开一个线程

import websocket
import datetime
import hashlib
import base64
import hmac
import json
from urllib.parse import urlencode
import time
import ssl
from wsgiref.handlers import format_date_time
from datetime import datetime
from time import mktime
import _thread as thread
import pyaudio
import io
import speech_recognition as sr
from opcua import Client
from opcua import ua
import pyttsx3
import queue
import config

import concurrent.futures

STATUS_FIRST_FRAME = 0  # 第一帧的标识
STATUS_CONTINUE_FRAME = 1  # 中间帧标识
STATUS_LAST_FRAME = 2  # 最后一帧的标识
IS_WAKEN_MODE = False  # 唤醒模式
ASR_WAKE_WORD = ""  # 语音转换的结果


class Ws_Param(object):
    # 初始化
    def __init__(self, APPID, APIKey, APISecret):
        self.APPID = APPID
        self.APIKey = APIKey
        self.APISecret = APISecret

        # 公共参数(common)
        self.CommonArgs = {"app_id": self.APPID}
        # 业务参数(business)，更多个性化参数可在官网查看
        self.BusinessArgs = {"domain": "iat",
                             "language": "zh_cn",
                             "accent": "mandarin",
                             "vinfo": 1,
                             "ptt": 0,
                             "vad_eos": 10000}

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


# 唤醒词判断
def is_wake_word(asr_txt):
    # A list of wake words
    wake_words_collection = ['小张', '小章', '嚣张']
    # Check to see if the users command/text contains a wake word/phrase
    for wake_word in wake_words_collection:
        if wake_word in asr_txt:
            return True
    # If the wake word isn't found in the text from the loop and so it returns False
    return False


# 收到websocket消息的处理
def on_message(ws, message):
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

            if result == '':
                pass
            else:
                global ASR_WAKE_WORD
                ASR_WAKE_WORD = result

                # print(f'讯飞: {ASR_WAKE_WORD}')

                global IS_WAKEN_MODE
                IS_WAKEN_MODE = is_wake_word(ASR_WAKE_WORD)

                # main()==========================================
                if IS_WAKEN_MODE:
                    # 回答“我在”
                    q_speak.put_nowait("我在")
                    # 复位唤醒
                    IS_WAKEN_MODE = False
                    ASR_WAKE_WORD = ""
                    # 语音转文字
                    try:
                        asr_txt = one_asr()
                        # asr_txt = ASR_WAKE_WORD
                        q_hmi_you.put_nowait(asr_txt)
                        # 意图分析
                        intent_txt = intent_for_send(asr_txt)
                        print(f'意图是：{intent_txt}')
                        q_hmi_sva.put_nowait(intent_txt)
                        if intent_txt is None:
                            # 回答“想好说什么再喊我”
                            q_speak.put_nowait("想好说什么再喊我")
                        else:
                            # 回答“好的”
                            q_speak.put_nowait("好的")
                            q_opc.put_nowait(intent_txt)
                    except sr.WaitTimeoutError:
                        # 回答“没别的事，我休息一下”
                        q_speak.put_nowait('没别的事，我休息一下')
                else:
                    pass
                    # print(f'讯飞: {ASR_WAKE_WORD}')
                    # q_hmi_you.put_nowait(ASR_WAKE_WORD)
    except Exception as e:
        print("receive msg,but parse exception:", e)


# 收到websocket错误的处理
def on_error(ws, error):
    print("### error:", error)


# 收到websocket关闭的处理
def on_close(ws):
    print("### closed ###")


# 收到websocket连接建立的处理
def on_open(ws):
    def run(*args):
        intervel = 0.04  # 发送音频间隔(单位:s)
        status = STATUS_FIRST_FRAME  # 音频的状态信息，标识音频是第一帧，还是中间帧、最后一帧

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
        print('---------------等待唤醒---------------')

        pack_num = int(RATE / CHUNK * 30)
        try:
            for i in range(0, pack_num):
                buf = stream.read(CHUNK)
                # if IS_WAKEN_MODE:
                #     break
                # # 文件结束
                if i == (pack_num - 1):
                # if i == (pack_num - 1) or IS_WAKEN_MODE:
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
                    ws.send(d)
                    status = STATUS_CONTINUE_FRAME
                # 中间帧处理
                elif status == STATUS_CONTINUE_FRAME:
                    d = {"data": {"status": 1, "format": "audio/L16;rate=16000",
                                  "audio": str(base64.b64encode(buf), 'utf-8'),
                                  "encoding": "raw"}}
                    ws.send(json.dumps(d))
                # 最后一帧处理
                elif status == STATUS_LAST_FRAME:
                    d = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                                  "audio": str(base64.b64encode(buf), 'utf-8'),
                                  "encoding": "raw"}}
                    ws.send(json.dumps(d))
                    # print('---------------last----------------')
                    time.sleep(1)
                    break
                # 模拟音频采样间隔
                time.sleep(intervel)
        except Exception as e:
            print("receive msg,but parse exception:", e)

        stream.stop_stream()
        stream.close()
        p.terminate()

        ws.close()

    # if not IS_WAKEN_MODE:
    thread.start_new_thread(run, ())


def one_asr():
    recognizer = sr.Recognizer()
    microphone = sr.Microphone(sample_rate=16000)
    with microphone as source:
        print('say something')
        # recognizer.adjust_for_ambient_noise(source)
        audio = recognizer.listen(source, timeout=6.0, phrase_time_limit=20.0).get_raw_data()
    ws = websocket.create_connection(wsUrl)

    print("Sending 'audio'...")
    frameSize = 8000  # 每一帧的音频大小
    intervel = 0.04  # 发送音频间隔(单位:s)
    status = STATUS_FIRST_FRAME  # 音频的状态信息，标识音频是第一帧，还是中间帧、最后一帧

    fp = io.BytesIO()
    fp.write(audio)
    fp.seek(0)

    while True:
        buf = fp.read(frameSize)
        # 文件结束
        if not buf:
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
            ws.send(d)
            status = STATUS_CONTINUE_FRAME
        # 中间帧处理
        elif status == STATUS_CONTINUE_FRAME:
            d = {"data": {"status": 1, "format": "audio/L16;rate=16000",
                          "audio": str(base64.b64encode(buf), 'utf-8'),
                          "encoding": "raw"}}
            ws.send(json.dumps(d))
        # 最后一帧处理
        elif status == STATUS_LAST_FRAME:
            d = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                          "audio": str(base64.b64encode(buf), 'utf-8'),
                          "encoding": "raw"}}
            ws.send(json.dumps(d))
            time.sleep(0.5)
            break
        # 模拟音频采样间隔
        time.sleep(intervel)

    fp.close()

    print("Sent")
    print("Receiving...")
    message = ws.recv()
    ws.close()
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
            # print(f'命令是:{result}')
            return result
    except Exception as e:
        print("receive msg,but parse exception:", e)


# 意图分析子函数
def keyword_in_sentence(word_collection, sentence):
    keyword_include = False
    for word in word_collection:
        if word in sentence:
            keyword_include = True if keyword_include is False else True
    return keyword_include


# 意图分析
def intent_for_send(text):
    # 将句子转换为意图
    # 意图为主谓短语，如换辊小车锁紧、换辊小车松开
    # intent_phrase_zh = ['换辊小车左行', '换辊小车右行', '换辊小车锁紧', '换辊小车松开', '换辊推拉缸前进', '换辊推拉缸后退']
    # intent_mqtt = ['srm/car/left', 'srm/car/right', 'srm/car/lock', 'srm/car/unlock', 'srm/beam/fwd', 'srm/beam/bwd']
    # intent_match = dict(zip(intent_phrase_zh, intent_mqtt))

    # 关键词模糊匹配
    # 换辊小车
    car_words_collection = ['车']
    # 左
    left_words_collection = ['左']
    # 右
    right_words_collection = ['右', '又']
    # 锁紧
    lock_words_collection = ['锁紧', '锁定', '缩紧', '缩进']
    # 松开
    unlock_words_collection = ['松开', '松', '解锁']
    # 换辊推拉缸
    cylinder_words_collection = ['缸', '液压缸', '推拉缸']
    # 前进
    fwd_words_collection = ['前', '前伸', '推进']
    # 后退
    bwd_words_collection = ['后', '退' '拉出', '推出']
    # 停止
    stop_words_collection = ['停', '挺']
    # 点动
    jog_words_collection = ['点']
    # 1秒
    s1_words_collection = ['1秒', '一秒', '疫苗']
    # 2秒
    s2_words_collection = ['2秒', '二秒', '两秒']
    # 3秒
    s3_words_collection = ['3秒', '三秒', '疫苗']
    # 4秒
    s4_words_collection = ['4秒', '四秒', '寺庙', '十秒']
    # 5秒
    s5_words_collection = ['5秒', '五秒']
    # 取消拆分功能 2020/08/29
    # # split the sentence with the word
    # seg = pkuseg.pkuseg()
    # seg_text = seg.cut(text)
    # print(f'拆分：{seg_text}')
    # 判断句子里是否有关键词
    get_keyword_car = keyword_in_sentence(car_words_collection, text)
    get_keyword_left = keyword_in_sentence(left_words_collection, text)
    get_keyword_right = keyword_in_sentence(right_words_collection, text)
    get_keyword_lock = keyword_in_sentence(lock_words_collection, text)
    get_keyword_unlock = keyword_in_sentence(unlock_words_collection, text)
    get_keyword_cylinder = keyword_in_sentence(cylinder_words_collection, text)
    get_keyword_fwd = keyword_in_sentence(fwd_words_collection, text)
    get_keyword_bwd = keyword_in_sentence(bwd_words_collection, text)
    get_keyword_stop = keyword_in_sentence(stop_words_collection, text)
    get_keyword_jog = keyword_in_sentence(jog_words_collection, text)
    ###
    get_keyword_1s = keyword_in_sentence(s1_words_collection, text)
    get_keyword_2s = keyword_in_sentence(s2_words_collection, text)
    get_keyword_3s = keyword_in_sentence(s3_words_collection, text)
    get_keyword_4s = keyword_in_sentence(s4_words_collection, text)
    get_keyword_5s = keyword_in_sentence(s5_words_collection, text)

    # 意图组合
    # TODO 设计成意图表格
    if get_keyword_car and get_keyword_left and not get_keyword_jog:
        if get_keyword_1s:
            intent_phrase = '换辊小车左行1秒'
        elif get_keyword_2s:
            intent_phrase = '换辊小车左行2秒'
        elif get_keyword_3s:
            intent_phrase = '换辊小车左行3秒'
        elif get_keyword_4s:
            intent_phrase = '换辊小车左行4秒'
        elif get_keyword_5s:
            intent_phrase = '换辊小车左行5秒'
        else:
            intent_phrase = '换辊小车左行'
    elif get_keyword_car and get_keyword_right and not get_keyword_jog:
        if get_keyword_1s:
            intent_phrase = '换辊小车右行1秒'
        elif get_keyword_2s:
            intent_phrase = '换辊小车右行2秒'
        elif get_keyword_3s:
            intent_phrase = '换辊小车右行3秒'
        elif get_keyword_4s:
            intent_phrase = '换辊小车右行4秒'
        elif get_keyword_5s:
            intent_phrase = '换辊小车右行5秒'
        else:
            intent_phrase = '换辊小车右行'
    elif get_keyword_car and get_keyword_stop:
        intent_phrase = '换辊小车停止'
    elif get_keyword_car and get_keyword_left and get_keyword_jog:
        intent_phrase = '换辊小车向左点动'
    elif get_keyword_car and get_keyword_right and get_keyword_jog:
        intent_phrase = '换辊小车向右点动'
    elif get_keyword_car and get_keyword_lock:
        intent_phrase = '换辊小车锁紧'
    elif get_keyword_car and get_keyword_unlock:
        intent_phrase = '换辊小车松开'
    elif get_keyword_cylinder and get_keyword_fwd and not get_keyword_jog:
        if get_keyword_1s:
            intent_phrase = '换辊推拉缸前进1秒'
        elif get_keyword_2s:
            intent_phrase = '换辊推拉缸前进2秒'
        elif get_keyword_3s:
            intent_phrase = '换辊推拉缸前进3秒'
        elif get_keyword_4s:
            intent_phrase = '换辊推拉缸前进4秒'
        elif get_keyword_5s:
            intent_phrase = '换辊推拉缸前进5秒'
        else:
            intent_phrase = '换辊推拉缸前进'
    elif get_keyword_cylinder and get_keyword_bwd and not get_keyword_jog:
        if get_keyword_1s:
            intent_phrase = '换辊推拉缸后退1秒'
        elif get_keyword_2s:
            intent_phrase = '换辊推拉缸后退2秒'
        elif get_keyword_3s:
            intent_phrase = '换辊推拉缸后退3秒'
        elif get_keyword_4s:
            intent_phrase = '换辊推拉缸后退4秒'
        elif get_keyword_5s:
            intent_phrase = '换辊推拉缸后退5秒'
        else:
            intent_phrase = '换辊推拉缸后退'
    elif get_keyword_cylinder and get_keyword_stop:
        intent_phrase = '换辊推拉缸停止'
    elif get_keyword_cylinder and get_keyword_fwd and get_keyword_jog:
        intent_phrase = '换辊推拉缸点动前进'
    elif get_keyword_cylinder and get_keyword_bwd and get_keyword_jog:
        intent_phrase = '换辊推拉缸点动后退'
    else:
        intent_phrase = None
    # 返回报文
    # intent_for_publish = intent_match.get(intent_phrase)
    # print(intent_for_publish)
    # return intent_for_publish
    # 返回句子
    # TODO 句子成分缺失追问
    return intent_phrase


# 与PLC通讯--接收
def opc_rev_plc():
    url = "opc.tcp://192.168.10.1:4840"
    snd_nodeid = 'ns=3;s="OPCUA_SND"."srm_mqtt_pub"'
    snd_flg_nodeid = 'ns=3;s="OPCUA_SND"."srm_mqtt_pub_flg"'

    with Client(url=url) as client:
        # 接收自PLC节点ID
        snd_srm_mqtt_pub = client.get_node(snd_nodeid)
        snd_srm_mqtt_pub_flg = client.get_node(snd_flg_nodeid)

        # 初始化同步PLC的数据
        # 接收自PLC
        # snd_srm_mqtt_pub_value = snd_srm_mqtt_pub.get_value()
        snd_srm_mqtt_pub_flg_value = snd_srm_mqtt_pub_flg.get_value()
        snd_srm_mqtt_pub_flg_value_old = snd_srm_mqtt_pub_flg_value

        while True:
            time.sleep(0.1)
            # 接收PLC消息
            snd_srm_mqtt_pub_value = snd_srm_mqtt_pub.get_value()
            snd_srm_mqtt_pub_flg_value = snd_srm_mqtt_pub_flg.get_value()
            if snd_srm_mqtt_pub_value == '':
                pass
            elif snd_srm_mqtt_pub_flg_value != snd_srm_mqtt_pub_flg_value_old:
                print("PLC: "+snd_srm_mqtt_pub_value)
                if q_speak.empty():
                    q_speak.put(snd_srm_mqtt_pub_value)
                else:
                    pass
            snd_srm_mqtt_pub_flg_value_old = snd_srm_mqtt_pub_flg_value


# 与PLC通讯--发送
def opc_snd_plc():
    url = "opc.tcp://192.168.10.1:4840"
    rev_nodeid = 'ns=3;s="OPCUA_REV"."srm_mqtt_sub"'
    rev_flg_nodeid = 'ns=3;s="OPCUA_REV"."srm_mqtt_sub_flg"'

    with Client(url=url) as client:
        # 发送到PLC节点ID
        rev_srm_mqtt_sub = client.get_node(rev_nodeid)
        rev_srm_mqtt_sub_flg = client.get_node(rev_flg_nodeid)

        # 初始化同步PLC的数据
        # 发送给PLC
        rev_srm_mqtt_sub_flg_value = rev_srm_mqtt_sub_flg.get_value()

        while True:
            # 发送PLC消息
            rev_srm_mqtt_sub_value = q_opc.get()
            rev_srm_mqtt_sub.set_value(ua.DataValue(ua.Variant(rev_srm_mqtt_sub_value, ua.VariantType.String)))
            if rev_srm_mqtt_sub_flg_value >= 32767:
                rev_srm_mqtt_sub_flg_value = -32768
            else:
                rev_srm_mqtt_sub_flg_value = rev_srm_mqtt_sub_flg_value + 1
            rev_srm_mqtt_sub_flg.set_value(ua.DataValue(ua.Variant(rev_srm_mqtt_sub_flg_value, ua.VariantType.Int16)))
            q_opc.task_done()


# 与PLC通讯--显示
def opc_hmi_you():
    url = "opc.tcp://192.168.10.1:4840"
    you_nodeid = 'ns=3;s="HMI_IF"."hmi_you_speak"'

    with Client(url=url) as client:
        # 发送HMI显示你说节点ID
        hmi_you_speak = client.get_node(you_nodeid)

        while True:
            hmi_you_speak_value = q_hmi_you.get()
            hmi_you_speak.set_value(ua.DataValue(ua.Variant(hmi_you_speak_value, ua.VariantType.String)))
            q_hmi_you.task_done()


# 与PLC通讯--显示
def opc_hmi_sva():
    url = "opc.tcp://192.168.10.1:4840"
    sva_nodeid = 'ns=3;s="HMI_IF"."hmi_sva_speak"'

    with Client(url=url) as client:
        # 发送HMI显示小张说节点ID
        hmi_sva_speak = client.get_node(sva_nodeid)

        while True:
            hmi_sva_speak_value = q_hmi_sva.get()
            hmi_sva_speak.set_value(ua.DataValue(ua.Variant(hmi_sva_speak_value, ua.VariantType.String)))
            q_hmi_sva.task_done()


# 发声
def tts():
    while True:
        text = q_speak.get()
        # print(f'发声{text}')
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        q_speak.task_done()


def wake():
    global IS_WAKEN_MODE
    while True:
        if not IS_WAKEN_MODE:
            time1 = datetime.now()
            ws = websocket.WebSocketApp(wsUrl, on_message=on_message, on_error=on_error, on_close=on_close)
            ws.on_open = on_open
            ws.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE})
            time2 = datetime.now()
            print(time2 - time1)


if __name__ == "__main__":
    wsParam = Ws_Param(APPID=config.APPID, APIKey=config.APIKey,
                       APISecret=config.APISecret)
    websocket.enableTrace(False)
    wsUrl = wsParam.create_url()
    q_speak = queue.Queue()
    q_opc = queue.Queue()
    q_hmi_sva = queue.Queue()
    q_hmi_you = queue.Queue()
    with concurrent.futures.ThreadPoolExecutor() as executor:
        f1 = executor.submit(opc_rev_plc)
        f2 = executor.submit(opc_snd_plc)
        f3 = executor.submit(opc_hmi_you)
        f4 = executor.submit(opc_hmi_sva)
        f5 = executor.submit(tts)
        wake()
