from telegram.ext import (Application, CommandHandler, MessageHandler,
                          filters, ContextTypes, ConversationHandler)
from telegram import Update, constants
from telegram.error import TimedOut, BadRequest
import openai
import asyncio
import mysql.connector
import os
import logging
import time
import uuid
import redis
import json
import requests
from wrapt_timeout_decorator import *
from types import GeneratorType
from datetime import date, timedelta

db_config = {
    'host': os.environ['DB_HOST'],
    'user': os.environ['DB_USER'],
    'password': os.environ['DB_PWD'],
    'port': os.environ['DB_PORT']
}

redis1 = redis.Redis(host=os.environ['REDIS_HOST'],
                     password=os.environ['REDIS_PASSWORD'],
                     port=os.environ['REDIS_PORT'])


def select_all(sql) -> list:
    logging.info(sql)
    db = mysql.connector.connect(**db_config)
    cursor = db.cursor()
    cursor.execute(sql)
    rows = cursor.fetchall()
    cursor.close()
    db.close()
    return rows


def select_one(sql):
    logging.info(sql)
    db = mysql.connector.connect(**db_config)
    cursor = db.cursor()
    cursor.execute(sql)
    row = cursor.fetchone()
    cursor.close()
    db.close()
    return row


def execute_sql(sql) -> None:
    logging.info(sql)
    db = mysql.connector.connect(**db_config)
    cursor = db.cursor()
    cursor.execute(sql)
    db.commit()
    cursor.close()
    db.close()


def init_database():
    logging.info("init database")
    while True:
        try:
            time.sleep(2)
            db = mysql.connector.connect(**db_config)
            break
        except Exception as e:
            logging.info("MySQL is not activate yet, try reconnect.")

    execute_sql(f"CREATE DATABASE IF NOT EXISTS {os.environ['DATABASE']};")

    db_config["database"] = os.environ['DATABASE']

    logging.info(db_config)

    execute_sql("DROP TABLE IF EXISTS images;")

    execute_sql(
        "CREATE TABLE images (id serial PRIMARY KEY, prompt TEXT, image VARCHAR(255));")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a message when the command /help is issued."""

    help_message = "Hello, I'm a smart chatbot powered by ChatGPT.\n" +\
        "I have prepared some interesting commands for you:\n\n" +\
        "/usage: Check the total usage of your account.\n\n" +\
        "/start: Start a new conversation with a context\n\n" +\
        "/end: Finish current conversation\n\n" +\
        "/image: Enter a prompt, I can generate a realistic image for you, the image will be saved in the database\n" +\
        "Example: /image a lovely cat\n\n" +\
        "/image_log: List the history of generated images.\n\n" +\
        "/image_review: Enter an id of a image record, you can check the generated image again\n" +\
        "Example: /image_review 4\n\n" +\
        "/image_del: Delete an image record from database"

    logging.info("help command")
    await update.message.reply_text(help_message)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.info("start command")
    id = update.message.from_user.id
    context_json = json.dumps(
        [{"role": "system", "content": "You are a helpful chatbot"}])
    redis1.set(id, context_json)
    await update.message.reply_text("Hello, what can I do for you?")


async def end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    id = update.message.from_user.id
    logging.info("end command")
    redis1.delete(id)
    await update.message.reply_text("Good bye~~")


@timeout(10)
def make_request(messages) -> GeneratorType:
    response_gen = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=messages,
        stream=True,
        temperature=0.7
    )
    return response_gen


def message_generator(msg, id) -> GeneratorType:
    data = redis1.get(id)
    result = ""
    if data is None:
        logging.info(f"User id={id}, Not using context:\n{msg}")
        messages = [{"role": "user", "content": msg}]
        response_gen = make_request(messages)

        for chunk in response_gen:
            delta = chunk.choices[0].delta
            if "content" in delta:
                result += delta.get("content")
                yield 0, result
        yield 1, result
    else:
        context = json.loads(data)
        assert isinstance(context, list)

        context.append({"role": "user", "content": msg})
        logging.info(f'User id={id}, Using context, context:\n{context}')

        response_gen = make_request(context)

        for chunk in response_gen:
            delta = chunk.choices[0].delta
            if "content" in delta:
                result += delta.get("content")
                yield 0, result

        context.append({"role": "assistant", "content": result})

        redis1.set(id, json.dumps(context))

        result += '\n\n\nYou are chat me with a context, please remember to use /end command to stop the conversation.'

        yield 1, result


async def gpt_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = time.time()
    try:
        placeholder_message = await update.message.reply_text("...")
        await update.message.chat.send_action(action="typing")

        id = update.message.from_user.id
        msg = update.message.text
        msg_gen = message_generator(msg, id)
        pre_len = 0
        for finish, msg in msg_gen:
            if not finish and len(msg) - pre_len < 40:
                continue
            try:
                parse_mode = (constants.ParseMode.MARKDOWN, None)['```' not in msg or not finish]
                await placeholder_message.edit_text(msg, parse_mode=parse_mode)
            except BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    logging.info("unmodified message")
                    continue
                else:
                    logging.info(f"{e}\nre-send message")
                    await placeholder_message.edit_text(msg, parse_mode=parse_mode)

            # sleep 0.03s to avoid flood error of telegram
            await asyncio.sleep(0.03)
            pre_len = len(msg)

        logging.info(f"Request cost {time.time() - start}seconds")

    except (openai.error.Timeout, TimedOut, TimeoutError) as e:
        logging.info(str(e))
        await update.message.reply_text(
            f"Time out with chatbot, please retry!")
    except Exception as e:
        module = e.__class__.__module__
        if module is None or module == str.__class__.__module__:
            text = e.__class__.__name__
        text = module + '.' + e.__class__.__name__
        logging.info(str(text) + str(e))
        await update.message.reply_text(
            f"Something wrong with chatbot, please retry!")


@timeout(25)
def image(prompt) -> str:
    response = openai.Image.create(
        prompt=prompt,
        n=1,
        size="1024x1024"
    )
    image_url = response['data'][0]['url']
    return image_url


async def download_img(url):
    rsp = requests.get(url)
    img = rsp.content
    rsp.close()
    return img


async def save_image(prompt: str, img: bytes):
    file_name = f"{uuid.uuid4()}.jpg"
    with open(f'/images/{file_name}', 'wb') as f:
        f.write(img)

    sql = f'INSERT INTO images (prompt, image) VALUES ("{prompt}", "{file_name}");'
    execute_sql(sql)


async def image_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please enter a prompt")
        return
    try:
        prompt = " ".join(context.args)
        logging.info(f"/image:{prompt}")
        start = time.time()
        image_url = image(prompt)
        logging.info(f"Request cost {time.time() - start}seconds")
        img = await download_img(image_url)
        await save_image(prompt, img)
        await update.message.reply_photo(
            img, caption="Here is the picture generating for you. I already save it in database, you can type /image_log to ckeck the history.\n")

    except (openai.error.Timeout, TimedOut, TimeoutError) as e:
        logging.info(str(e))
        await update.message.reply_text(
            f"Time out with chatbot, please retry!")
    except Exception as e:
        module = e.__class__.__module__
        if module is None or module == str.__class__.__module__:
            text = e.__class__.__name__
        text = module + '.' + e.__class__.__name__
        logging.info(str(text) + str(e))
        await update.message.reply_text(
            f"Something wrong with chatbot, please retry!")


async def image_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        sql = f'select * from images;'

        imgs = select_all(sql)

        result = 'Here are the image records:\n'
        for img in imgs:
            result += f'{img[0]}. {img[1]}\n'
        result += "\n\nYou can use /image_review command to check an image record."
        result += "\nYou can use /image_del command to delete an image record."
        await update.message.reply_text(result)
    except Exception as e:
        logging.info(e)
        await update.message.reply_text("Something wrong with database!")


async def image_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text("Please Enter the id of an image record!")
        return
    id = context.args[0]
    if not id.isdigit():
        await update.message.reply_text(
            "To review an image, please enter the id of it, use /image_log command to check the id.")
        return
    id = int(id)
    sql = f'SELECT * FROM images WHERE id={id};'
    try:
        row = select_one(sql)
    except Exception as e:
        logging.info(e)
        await update.message.reply_text(
            "Something wrong with the database.")

    if row is not None:
        file_name = row[2]
        with open(f'/images/{file_name}', 'rb') as f:
            img = f.read()
        await update.message.reply_photo(
            img, caption="Here is the image you want review.\n")
    else:
        await update.message.reply_text(
            f"I'm sorry, I can't find this record...")


async def image_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await update.message.reply_text(
            "To delete an image record, please enter the id of it.")
        return

    arg = context.args[0]
    if not arg.isdigit():
        await update.message.reply_text(
            "To delete an image record, please enter the id of it.")
        return
    id = int(arg)
    sql = f'DELETE FROM images WHERE id={id};'

    try:
        execute_sql(sql)
        await update.message.reply_text("Successfully delete an image record!")
    except Exception as e:
        logging.info(e)
        await update.message.reply_text(
            'Failed to delete an item, please try again!')


async def usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        today = date.today() + timedelta(days=1)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15"
        }
        url = f'https://api.openai.com/v1/dashboard/billing/usage?start_date=2023-04-01&&end_date={today}'
        r = requests.get(url=url, headers=headers)
        data = json.loads(r.content)
        logging.info(data)
        usage = data['total_usage']
        text = f"Your account usage: ${usage/100:.3f} / $5.00"
        text += "\nPricing of GPT-3.5-turbo: $0.01 / 5000 tokens"
        await update.message.reply_text(text)
    except Exception as e:
        module = e.__class__.__module__
        if module is None or module == str.__class__.__module__:
            text = e.__class__.__name__
        text = module + '.' + e.__class__.__name__
        logging.info(str(text) + str(e))
        await update.message.reply_text(
            f"Something wrong with chatbot, please retry!")
    finally:
        r.close()


def main():
    application = Application.builder().token(
        os.environ["ACCESS_TOKEN"]).concurrent_updates(True).build()

    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                        level=logging.INFO)

    init_database()

    application.add_handler(CommandHandler("help", help_command, block=False))
    application.add_handler(CommandHandler("start", start, block=False))
    application.add_handler(CommandHandler("end", end, block=False))
    application.add_handler(MessageHandler(
        filters.TEXT & (~filters.COMMAND), gpt_reply, block=False))
    application.add_handler(CommandHandler("image", image_reply, block=False))
    application.add_handler(CommandHandler(
        "image_log", image_list, block=False))
    application.add_handler(CommandHandler(
        "image_review", image_review, block=False))
    application.add_handler(CommandHandler(
        "image_del", image_del, block=False))
    application.add_handler(CommandHandler(
        "usage", usage, block=False))

    # To start the bot:
    application.run_polling()


if __name__ == "__main__":
    main()
