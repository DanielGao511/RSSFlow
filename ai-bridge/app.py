import os
import redis
import feedparser
from flask import Flask, request, Response
from openai import OpenAI
from dotenv import load_dotenv
import hashlib
import re
import concurrent.futures  # ✅ 引入并发库
import socket
import urllib.parse

# 加载环境变量
load_dotenv()
app = Flask(__name__)

# --- 配置 ---
ALIYUN_API_KEY = os.getenv("ALIYUN_API_KEY")
ALIYUN_BASE_URL = os.getenv("ALIYUN_BASE_URL")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))

# ✅ 全局 socket 超时设置为 120秒，防止 RSSHub 抓取长文时 AI-Bridge 提前断开
socket.setdefaulttimeout(120)

# --- 初始化 Redis ---
try:
    cache = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=0, decode_responses=True)
    cache.ping()
    print("✅ Redis 连接成功")
except Exception as e:
    print(f"⚠️ Redis 连接失败: {e}")
    cache = None


def get_ai_processing(title, content):
    """
    调用 AI 进行翻译和总结的核心函数
    """
    # 1. 检查缓存
    if not cache:
        return title, "Redis Error (No Cache)", content

    content_hash = hashlib.md5((title + content[:200]).encode()).hexdigest()
    cache_key = f"ai_result_v17_aliyun:{content_hash}"

    cached = cache.get(cache_key)
    if cached:
        try:
            parts = cached.split("|||")
            if len(parts) == 2:
                return parts[0], parts[1]
        except:
            pass

    # 2. 准备 Prompt
    system_prompt = (
        "你是一个专业的新闻主编。请将新闻翻译为中文，并重构为适合阅读的干净HTML格式。"
    )
    user_prompt = f"""
    请严格按照以下格式输出，中间用 ||| 分隔两个部分：
    1. 中文翻译后的原文标题
    2. 中文全文翻译（必须遵守以下HTML清洗规则）

    【HTML清洗与翻译规则】：
    - **严禁使用** <div, <span, <nav, <style> 标签。
    - **严禁保留** 任何 class="...", style="...", id="..." 属性。
    - 正文段落必须用 <p> 标签包裹。
    - 小标题使用 <h3> 或 <h4> 标签。
    - 仅保留 <img>, <p>, <b>, <strong>, <blockquote>, <ul>, <li>, <a> 这些基础标签。
    - 确保图片链接 <img> 完整保留。

    原文标题：{title}
    原文内容：{content} 
    """

    try:
        if not ALIYUN_API_KEY:
            raise ValueError("ALIYUN_API_KEY 环境变量未设置！")

        client = OpenAI(api_key=ALIYUN_API_KEY, base_url=ALIYUN_BASE_URL)

        completion = client.chat.completions.create(
            model=os.getenv("MODEL_NAME"),
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            timeout=120,  # AI 请求本身的超时
        )
        result = completion.choices[0].message.content.strip()

        # 清洗
        result = result.replace("```html", "").replace("```", "")
        parts = result.split("|||")

        cn_title = parts[0].strip() if len(parts) > 0 else title
        cn_content = parts[1].strip() if len(parts) > 1 else content

        # 写入缓存
        cache.setex(cache_key, 604800, f"{cn_title}|||{cn_content}")
        return cn_title, cn_content

    except Exception as e:
        print(f"❌ Aliyun Error processing {title[:10]}: {e}")
        return (
            title,
            "⚠️ AI服务异常",
            f"错误详情: {str(e)}<br><br>原始内容:<br>{content}",
        )


def extract_first_image(html_content):
    if not html_content:
        return None
    match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', html_content)
    return match.group(1) if match else None


def generate_xml(entries, original_feed):
    xml = ['<?xml version="1.0" encoding="UTF-8" ?>']
    xml.append(
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
    )
    xml.append("<channel>")
    feed_title = original_feed.feed.get("title", "Unknown Feed")
    xml.append(f"<title>qwen AI - {feed_title}</title>")

    for entry in entries:
        xml.append("<item>")
        xml.append(f'<title><![CDATA[{entry["cn_title"]}]]></title>')
        xml.append(f'<link>{entry["link"]}</link>')
        xml.append(f'<description><![CDATA[{entry["cn_content"]}]]></description>')
        xml.append(
            f'<content:encoded><![CDATA[{entry["cn_content"]}]]></content:encoded>'
        )
        xml.append(f'<guid>{entry.get("id", entry["link"])}</guid>')
        xml.append("</item>")

    xml.append("</channel></rss>")
    return "".join(xml)


def process_single_entry(args):
    """
    工作线程函数：处理单个条目
    """
    index, entry = args

    title = entry.get("title", "无标题")
    link = entry.get("link", "")
    # 优先取 description，有些 RSS 源 content 在 summary 里
    raw_content = entry.get("description") or entry.get("summary") or ""

    # 调用 AI
    cn_title, cn_content = get_ai_processing(title, raw_content)

    img_url = extract_first_image(raw_content)
    img_tag = f'<img src="{img_url}"><br>' if img_url else ""

    return {
        "index": index,
        "cn_title": cn_title,
        "link": link,
        "cn_content": cn_content,
        "id": entry.get("id", link),
    }


@app.route("/feed")
def proxy_feed():
    target_url = request.args.get("url")
    if not target_url:
        return "Missing url", 400

    remaining_args = request.args.copy()
    remaining_args.pop("url")  # 移除已经获取的 url 参数本身

    if remaining_args:
        # 判断连接符：如果 target_url 原本就有 '?'，后面就用 '&' 拼接，否则用 '?'
        connector = "&" if "?" in target_url else "?"
        extra_params = []
        for key, value in remaining_args.items():
            extra_params.append(f"{key}={value}")

        target_url += f"{connector}{'&'.join(extra_params)}"
    print(f"📥 正在抓取: {target_url}")  # 👈 现在看日志，这里应该会有 ?mode=fulltext 了

    try:
        # 解析 RSS
        feed = feedparser.parse(target_url)
        # 简单的错误检查 (注意：有些源虽然成功但 bozo 也是 1，所以这里只做记录不强制报错)
        if not feed.entries and feed.bozo:
            print(f"⚠️ RSS Parse Warning: {feed.bozo_exception}")
    except Exception as e:
        return f"Error fetching feed: {str(e)}", 500

    target_entries = feed.entries
    if not target_entries:
        return Response(generate_xml([], feed), mimetype="application/xml")

    print(f"⚡ 开始并发处理 {len(target_entries)} 条内容...")

    processed_results = []
    # 准备带索引的任务，确保最后能排回来
    tasks = [(i, entry) for i, entry in enumerate(target_entries)]

    # === ✅ 并发执行 ===
    # max_workers=5 意味着 5 篇文章同时跑，速度提升 5 倍
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        future_to_entry = {
            executor.submit(process_single_entry, task): task for task in tasks
        }

        for future in concurrent.futures.as_completed(future_to_entry):
            try:
                data = future.result()
                processed_results.append(data)
            except Exception as exc:
                print(f"Task generated an exception: {exc}")

    # 按原始顺序重新排序
    processed_results.sort(key=lambda x: x["index"])

    print(f"✅ 处理完成，返回 XML")
    return Response(generate_xml(processed_results, feed), mimetype="application/xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
