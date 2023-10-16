from flask import Flask, request, jsonify, make_response, render_template, redirect, url_for, stream_with_context, Response
from flask_httpauth import HTTPBasicAuth
from flask_caching import Cache
from flask_cors import CORS
import openai
from openai.error import ServiceUnavailableError
import tiktoken
from collections import deque
import numpy as np
import faiss
import json
from datetime import datetime, timedelta
import csv
from langdetect import detect
import validators
import os
import re
from sklearn.metrics.pairwise import cosine_similarity
import threading

openai.api_key = ""

app = Flask(__name__)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})
CORS(app)

# スレッドローカル変数の定義
local_data = threading.local()

# Global variables
max_requests = float('inf')
reset_time = 10800
threshold = 0.7
model = 'gpt-3.5-turbo-16k'
knowledge_about_user = ""
response_preference = ""
log_option = 'off'
history_maxlen = 12
USERNAME = 'admin'
PASSWORD = 'password'
questions = []
corresponding_ids = []
folder_name = ""
settings_path = ""

# Cognito IDから動的にフォルダー名を設定する
def generate_folder_name(cognito_user_id):
    return f"dir_{cognito_user_id}"

def update_settings_path():
    global settings_path
    folder_name = local_data.folder_name
    settings_path = os.path.join(os.path.dirname(__file__), folder_name, 'settings.json')
    
# reference.jsonの読み込み
def get_file_path():
    global file_path, data, documents
    folder_name = local_data.folder_name
    file_path = os.path.join(os.path.dirname(__file__), folder_name, 'reference.json')
    with open(file_path, 'r') as f:
        data = json.load(f)
        documents = data

# vectors.npyの読み込み
dimension = 1536
index = None
vectors = None

def load_vectors_and_create_index():
    global vectors_path, vectors, index
    folder_name = local_data.folder_name
    vectors_path = os.path.join(os.path.dirname(__file__), folder_name, 'vectors.npy')
    vectors = np.load(vectors_path)
    
    index = faiss.IndexFlatL2(dimension)
    index.add(vectors)
    
def set_cognito_data(cognito_user_id):
    # フォルダ名を動的に生成
    folder_name = generate_folder_name(cognito_user_id)
    
    # スレッドローカル変数に値を設定
    local_data.folder_name = folder_name

    # settings_pathを更新
    update_settings_path()
    
    # 設定をロード
    load_config(cognito_user_id)
    
    # reference.jsonのpath更新
    get_file_path()
    
    # vectors_pathの更新
    load_vectors_and_create_index()

@app.route('/get_cognito_id', methods=['POST'])
def get_cognito_id_route():
    cognito_user_id = request.form.get("member_id")
    set_cognito_data(cognito_user_id)
    return cognito_user_id

user_settings = {}
def load_config(cognito_user_id):
    try:
        with open(settings_path, 'r') as f:
            config = json.load(f)
            user_settings[cognito_user_id] = {
                'api_key': config.get('api_key', openai.api_key),
                'max_requests': float(config.get('max_requests', max_requests)),
                'reset_time': int(config.get('reset_time', reset_time)),
                'threshold': float(config.get('threshold', threshold)),
                'model': config.get('model', model),
                'knowledge_about_user': config.get('knowledge_about_user', knowledge_about_user),
                'response_preference': config.get('response_preference', response_preference),
                'log_option': config.get('log_option', log_option),
                'history_maxlen': int(config.get('history_maxlen', history_maxlen)) if config.get('history_maxlen', history_maxlen) != float('inf') else float('inf'),
                'USERNAME': config.get('USERNAME', 'admin'),
                'PASSWORD': config.get('PASSWORD', 'password'),
                'questions': config.get('questions', '').split("\n") if config.get('questions', '') else [],
                'corresponding_ids': config.get('corresponding_ids', '').split("\n") if config.get('corresponding_ids', '') else []
            }
    except FileNotFoundError:
        pass

auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    cognito_user_id = username
    load_config(cognito_user_id)
    user_specific_settings = user_settings.get(cognito_user_id, {})
    USERNAME = user_specific_settings.get('USERNAME', 'admin')
    PASSWORD = user_specific_settings.get('PASSWORD', 'password')
    print(f"Verifying for {cognito_user_id} - USERNAME: {USERNAME}, PASSWORD: {PASSWORD}")  # デバッグ用
    return username == USERNAME and password == PASSWORD

@app.route('/config/<string:cognito_user_id>', methods=['GET'])
@auth.login_required
def config(cognito_user_id):
    set_cognito_data(cognito_user_id)
    
    # まずはデフォルトの設定をロード
    settings = {
        'api_key': openai.api_key,
        'max_requests': max_requests,
        'reset_time': reset_time,
        'threshold': threshold,
        'model': model,
        'knowledge_about_user': knowledge_about_user,
        'response_preference': response_preference,        
        'log_option': log_option,
        'history_maxlen': history_maxlen,
        'USERNAME': USERNAME,
        'PASSWORD': PASSWORD,
        'questions': "\n".join(questions) if questions else '',
        'corresponding_ids': "\n".join(corresponding_ids) if corresponding_ids else ''
    }
    
    # user_settingsから該当するcognito_user_idの設定を取得して上書き
    user_specific_settings = user_settings.get(cognito_user_id, {})
    settings.update(user_specific_settings)
    
    # MAX_REQUESTSの小数点を省く
    if settings['max_requests'] == float('inf'):
        settings['max_requests'] = str(settings['max_requests'])
    else:
        settings['max_requests'] = str(int(settings['max_requests']))
    
    # Zip the questions and corresponding_ids
    questions_for_user = user_specific_settings.get('questions', questions)
    corresponding_ids_for_user = user_specific_settings.get('corresponding_ids', corresponding_ids)
    zipped_questions_ids = list(zip(questions_for_user, corresponding_ids_for_user))

    # Pass the zipped list to the template
    return render_template('config.html', cognito_user_id=cognito_user_id, zipped_questions_ids=zipped_questions_ids, **settings)

@app.route('/save_config/<string:cognito_user_id>', methods=['POST'])
def save_config(cognito_user_id):
    set_cognito_data(cognito_user_id)
    # local variables
    api_key = request.form.get('api_key')
    max_requests_str = request.form.get('max_requests')
    max_requests = float(max_requests_str) if max_requests_str.strip() != '' else float('inf')
    reset_time = int(request.form.get('reset_time'))
    threshold_str = request.form.get('threshold')
    threshold = float(threshold_str) if threshold_str is not None else None
    model = request.form.get('model')
    knowledge_about_user = request.form.get('knowledge_about_user')
    response_preference = request.form.get('response_preference')
    log_option = request.form.get('log_option')
    history_maxlen_value = request.form.get('history_maxlen')
    history_maxlen = int(history_maxlen_value) if history_maxlen_value.strip() != '' else float('inf')
    username = request.form.get('USERNAME')
    password = request.form.get('PASSWORD')
    questions = request.form.getlist('questions[]')
    corresponding_ids = request.form.getlist('corresponding_ids[]')

    new_settings = {
        'api_key': api_key,
        'max_requests': max_requests,
        'reset_time': reset_time,
        'threshold': threshold,
        'model': model,
        'knowledge_about_user': knowledge_about_user,
        'response_preference': response_preference,  
        'log_option': log_option,
        'history_maxlen': history_maxlen,
        'USERNAME': username,
        'PASSWORD': password,
        'questions': "\n".join(questions),
        'corresponding_ids': "\n".join(corresponding_ids)
    }

    # Update user_settings dictionary
    user_settings[cognito_user_id] = new_settings

    # Save the settings to a JSON file
    with open(settings_path, 'w', encoding="utf-8") as f:
        json.dump(new_settings, f, ensure_ascii=False)

    # Redirect to the configuration page
    return redirect(url_for('config', cognito_user_id=cognito_user_id))

# OpenAIのモデルに対応するトークナイザを取得
enc = tiktoken.get_encoding("cl100k_base")

def count_tokens_with_tiktoken(text):
    return len(enc.encode(text))

def trim_to_tokens(text, max_tokens):
    tokens = enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    else:
        trimmed_tokens = tokens[:max_tokens]
        return enc.decode(trimmed_tokens)

# Initialize history and last active time as dictionaries
history = {}
last_active = {}

# ユーザーIDとリクエスト数を保存するパラメーター
user_requests = {}

def get_similar_faiss_id(headers, local_model, local_knowledge_about_user, user_message, user_id, history, prefix, combined_list):
    # 既存の履歴から必要なメッセージを抽出
    messages = []

    relevant_messages = [
        msg for msg in history.get(user_id, []) 
        if msg["role"] in ["user", "assistant"] and not msg["content"].startswith(prefix)
    ]

    # 過去の会話履歴が存在する場合のみ、過去の会話履歴を文字列として結合し、メッセージとして追加
    if relevant_messages:
        past_conversation = "\n".join([f"Role: {msg['role']}\nContent: {msg['content']}" for msg in relevant_messages])
        messages.append({"role": "system", "content": f"Here is the past conversation of this user, use it if it helps understanding the context of user's query:\n{past_conversation}\n"})
    
    # ユーザーの背景情報をシステムメッセージとして追加
    if local_knowledge_about_user:
        messages.append({
            "role": "system",
            "content": f"You are the knowledgeable assistant of following entity or association: \n{local_knowledge_about_user}\n"
        })
    
    # ユーザーのクエリに関する指示を追加
    instruction = (
        f"Given the user's query, identify and return the numeric ID(s) from the list of questions and their corresponding IDs: \n{combined_list}.\n\n"
        "If the context of the user's question matches any of the listed questions, return the corresponding ID(s). "
        "Remember to return ONLY the numeric ID(s). If there's no match, return 'None'. You MUST NOT reply to the user's message directly, NOR ASK for further clarification.\n"
    )
    
    messages.extend([
        {"role": "system", "content": instruction},
        {"role": "user", "content": user_message}
    ])
    
    # メッセージの内容を表示
    print("==== Messages List Content ====")
    for msg in messages:
        print(f"Role: {msg['role']}")
        if "questions and their corresponding IDs:" in msg['content']:
            pre_text, post_text = msg['content'].split("questions and their corresponding IDs:")
            print(f"Content: {pre_text}questions and their corresponding IDs:")
            print(post_text)  # combined_listの部分をそのまま表示
        else:
            print(f"Content: {msg['content']}")
    print("===============================")

    # OpenAIのAPIを使用してユーザーメッセージと質問の類似度を計算
    response = openai.ChatCompletion.create(
        model=local_model,
        messages=messages,
        headers=headers,
        temperature=0.0
    )
    print(f"OpenAI API Response: {response.choices[0].message['content']}")
    
    # レスポンスからIDを抽出
    matched_ids = re.findall(r'\d+', response.choices[0].message['content'])
    print(f"Matched IDs: {matched_ids}")
    
    return matched_ids[:2]  # 最初の2つのIDのみを返す

@app.route('/message', methods=['POST'])
def message():
    data = request.get_json()
    user_message = data['message']['text']
    user_id = data.get("user_id")
    session_id = data.get("session_id")
    cognito_user_id = data.get("member_id")
    stream = data.get("stream", False)
    
    # Lineなど、streamができないものに関してはここで各種pathやconfigを取得する
    if not stream:
        set_cognito_data(cognito_user_id)

    # cognito_user_idに基づいて設定を取得
    settings = user_settings.get(cognito_user_id, {})
    local_api_key = settings.get('api_key', openai.api_key)
    headers={"Authorization": f"Bearer {local_api_key}"}
    local_max_requests = settings.get('max_requests', max_requests)
    local_reset_time = settings.get('reset_time', reset_time)
    local_threshold = settings.get('threshold', threshold)
    local_model = settings.get('model', model)
    local_knowledge_about_user = settings.get('knowledge_about_user', knowledge_about_user)
    local_response_preference = settings.get('response_preference', response_preference)
    local_log_option = settings.get('log_option', log_option)
    local_history_maxlen = settings.get('history_maxlen', history_maxlen)
    local_questions = settings.get('questions', questions)
    local_corresponding_ids = settings.get('corresponding_ids', corresponding_ids)
    
    print(f"Cognito User ID Check: {cognito_user_id}")
    print(f"設定path: {settings_path}")
    print(f"ファイルpath: {file_path}")
    print(f"ベクトルpath: {vectors_path}")
    
    # Initialize the history for this user or session if it doesn't exist
    if user_id not in history:
        if local_history_maxlen == float('inf'):
            history[user_id] = deque()
        else:
            history[user_id] = deque(maxlen=local_history_maxlen)
        last_active[user_id] = datetime.now()
    else:
        print(f"Current history length for user {user_id}: {len(history[user_id])}")
    
    # Print the user ID and session ID
    print(f"User ID: {user_id}")
    print(f"Session ID: {session_id}")
    print(f"User message from user {user_id}: {user_message}") 

    # ユーザーIDが存在しない場合は初期化
    if user_id not in user_requests:
        user_requests[user_id] = {'count': 0, 'last_request': datetime.now()}

    # ユーザーのリクエスト数をチェック
    if user_requests[user_id]['count'] >= local_max_requests:
        # 最後のリクエストから一定時間が経過していれば会話履歴とMAX_REQUESTSをリセット
        if datetime.now() - user_requests[user_id]['last_request'] > timedelta(seconds=local_reset_time):
            user_requests[user_id] = {'count': 0, 'last_request': datetime.now()}
        else:
            return jsonify({"error": "Too many requests"}), 429
    
    # RESET_TIMEの秒数間inactiveの場合は履歴を削除
    if datetime.now() - user_requests[user_id]['last_request'] > timedelta(seconds=local_reset_time):
        history[user_id].clear()

    # リクエスト数を増やす
    user_requests[user_id]['count'] += 1
    user_requests[user_id]['last_request'] = datetime.now()
    
    # actual_titlesとactual_urlsを空のリストとして初期化
    actual_titles = []
    actual_urls = []
    
    # ユーザーメッセージの言語を検出
    language = detect(user_message)
    
    # メッセージのプレフィックスを設定
    if language == "ja":
        prefix = "データベースによると:"
    else:
        prefix = "According to our database:"
        
    # questionsとcorresponding_idsを組み合わせてプロンプトを作成
    combined_list = "\n".join([f"{q} - ID: {id_}" for q, id_ in zip(local_questions, local_corresponding_ids)])
    
    # matched_idsを空のリストとして初期化
    matched_ids = []
        
    # combined_listが空でない場合のみget_similar_faiss_id関数を呼び出す
    if combined_list:
        # get_similar_faiss_id関数を呼び出す
        matched_ids = get_similar_faiss_id(headers, local_model, local_knowledge_about_user, user_message, user_id, history, prefix, combined_list)
        print(f"Returned Matched IDs: {matched_ids}")
    else:
        print("No questions and corresponding IDs provided. Skipping the Q&IDs process.")
        
    # 参照IDを逆にする一時的なリスト（重要度が低い方から先にhistory.appendする）
    temp_references = []
    
    # マッチするIDが存在する場合、ベクトルの計算をスキップ
    if matched_ids:
        for matched_id in matched_ids:
            # IDを調整（CSVのオフセットを考慮）
            adjusted_id = int(matched_id) - 2
            
            # 該当するテキストをdocumentsから検索
            if adjusted_id < len(documents):
                matched_document = documents[adjusted_id]
                matched_text = matched_document["text"]
                matched_title = matched_document["title"]
                matched_url = matched_document["url"]
                
                # actual_titlesとactual_urlsに追加
                actual_titles.append(matched_title)
                actual_urls.append(matched_url)
                
                # temp_referenceに追加（後にhistory.appendされる）
                document_content = f"{matched_title} {matched_text}"
                temp_references.append({"role": "assistant", "content": f"{prefix} {document_content}"})

    else: 
        # Vectorize the user's message using OpenAI's model
        user_message_to_encode = user_message

        try:
            embedding_result = openai.Embedding.create(
                model="text-embedding-ada-002",
                input=user_message_to_encode,
                headers=headers
            )
        except Exception as e:
            print(f"Error while embedding user message: {e}")
            return jsonify({"error": "Failed to process user message."}), 500
        
        # print(f"Embedding result: {embedding_result}") # 不要になったら消す
        user_message_vector = embedding_result['data'][0]['embedding']

        # Query the index with the user's message vector
        try:
            D, I = index.search(np.array([user_message_vector], dtype=np.float32), 10)
        except Exception as e:
            print(f"Error while searching the index: {e}")
            return jsonify({"error": "Failed to search the database."}), 500

        # D contains the distances to the n closest vectors in the dataset
        closest_vector_distances = D[0]
        # print(f"Closest vector distances: {closest_vector_distances}")

        # I contains the indices of the n closest vectors in the dataset
        closest_vector_indices = I[0]

        # Get the documents corresponding to the closest vectors
        closest_documents = [documents[i]["text"] for i in closest_vector_indices]
        closest_titles = [documents[i]["title"] for i in closest_vector_indices]
        closest_urls = [documents[i]["url"] for i in closest_vector_indices]
        print(f"Closest document FAISS IDs (adjusted for CSV): {[idx + 2 for idx in closest_vector_indices]}")
        print(f"Closest document titles: {closest_titles}")
        # print(f"Closest document urls: {closest_urls}")

        # コサイン類似性を計算する部分を組み込む
        similarities = []
        for title, doc in zip(closest_titles, closest_documents):
            document_index = next((index for index, d in enumerate(documents) if d["title"] == title and d["text"] == doc), None)
            if document_index is not None:
                document_vector = vectors[document_index]
                similarity = cosine_similarity([document_vector], [user_message_vector])[0][0]
                similarities.append(similarity)
                # print(f"Title: {title}, Similarity: {similarity}")  # タイトルとその類似性を出力
            else:
                print(f"Document not found in reference list: {title}")
                similarities.append(0)

        # 距離スコアとコサイン類似性のスコアを組み合わせて新しいスコアを計算
        combined_scores = [0.5 * (1 - scaled_distance) + 0.5 * similarity for scaled_distance, similarity in zip(closest_vector_distances, similarities)]
        print("Combined scores (Scaled FAISS distance + Cosine similarity):", combined_scores)

        # Thresholdを超えたIDを履歴に追加
        # スコアと関連する情報を同時にソート
        sorted_indices = np.argsort(combined_scores)[::-2]  # スコアを降順でソート
        added_count = 0

        for idx in sorted_indices:
            score = combined_scores[idx]
            if score >= local_threshold:
                title = closest_titles[idx]
                url = closest_urls[idx]
                doc = closest_documents[idx]
                
                actual_titles.append(title)
                actual_urls.append(url)
                
                print("Actual Referred titles: ", title)
                print("Actual Referred urls: ", url)
                
                document_content = f"{title} {doc})"
                reference_content = f"{prefix} {document_content}"

                # temp_referenceに追加（後にhistory.appendされる）
                temp_references.append({"role": "assistant", "content": reference_content})
                
                added_count += 1
                if added_count >= 2:  # 上位2つの結果のみを保存
                    break

    # 一時的なリストを逆順にして、historyに追加
    for reference in reversed(temp_references):
        history[user_id].append(reference)
 
    # Calculate the total tokens for messages in history[user_id]
    total_tokens = sum(count_tokens_with_tiktoken(message["content"]) for message in history[user_id])
    print(f"Total tokens: {total_tokens}")
    
    # Set the token limit based on the model
    if local_model == 'gpt-4':
        token_limit = 8000
    elif local_model == 'gpt-3.5-turbo-16k':
        token_limit = 16000
    else:
        token_limit = 4000  # Default
        local_model
        
    print(f"Selected Model and its Token Limit: {local_model, token_limit}")

    # Define trimmed_content before the loop
    new_message = {"role": "assistant", "content": ""}
    trimmed_content = ""

    # While the total tokens exceed the limit, trim messages
    while total_tokens > token_limit:

        # If the oldest message is a reference from the assistant, prioritize trimming/removing it
        if history[user_id][0]["role"] == "assistant" and prefix in history[user_id][0]["content"]:
            tokens_to_remove = count_tokens_with_tiktoken(history[user_id][0]["content"])
            history[user_id].popleft()
            total_tokens -= tokens_to_remove

        # For any other messages, trim/remove it from the oldest
        else:
            tokens_to_remove = count_tokens_with_tiktoken(history[user_id][0]["content"])
            if tokens_to_remove > total_tokens - token_limit:
                trimmed_content = trim_to_tokens(history[user_id][0]["content"], total_tokens - token_limit)
                history[user_id][0]["content"] = trimmed_content
            else:
                history[user_id].popleft()
            total_tokens -= tokens_to_remove

    # Update and add the new AI message only if there was any trimming
    if trimmed_content:
        new_message["content"] = trimmed_content
        # Add the new AI message to history only if it's not empty
        history[user_id].append(new_message)
        
    # 最初のシステムメッセージでユーザーの立場や背景に関する情報を提供
    if local_knowledge_about_user:  # knowledge_about_userが空でない場合のみ追加
        knowledge_prompt = f"You are the knowledgeable assistant of following entity or association: {local_knowledge_about_user}"
        history[user_id].append({"role": "system", "content": knowledge_prompt})

    # 次のシステムメッセージで返信に関する指示
    if local_response_preference:  # response_preferenceが空でない場合のみ追加
        response_prompt = f"You must follow the response style with following instruction: {local_response_preference}"
        history[user_id].append({"role": "system", "content": response_prompt})
    
    # ユーザーのメッセージを履歴に追加
    history[user_id].append({"role": "user", "content": user_message})
    
    if not stream:
        try:
            response = openai.ChatCompletion.create(
                model=local_model,
                messages=list(history[user_id]),
                headers=headers
            )
        except ServiceUnavailableError:
            return jsonify({"error": "The server is overloaded or not ready yet. Please try again later."}), 503
        except Exception as e:
            print(f"Error while generating chat completion: {e}")
            return jsonify({"error": "Failed to generate a response."}), 500

        # AIのレスポンスに参照を追加する
        if actual_titles:
            references = ""
            for title, url in zip(actual_titles, actual_urls):
                if validators.url(url):
                    references += f'<br><br><a href="{url}" target="_blank">{title}</a>'
                else:
                    references += f'<br><br>{title} ({url})'
            new_message = {"role": "assistant", "content": response['choices'][0]['message']['content'] + references}
        else:
            new_message = {"role": "assistant", "content": response['choices'][0]['message']['content']}

        # トリム後のトークン数を確認
        new_message_tokens = count_tokens_with_tiktoken(new_message["content"])
        print(f"Tokens in final new_message (after AI response): {new_message_tokens}")

        # Check if the new message would cause the total tokens to exceed the limit
        while sum(count_tokens_with_tiktoken(message["content"]) for message in history[user_id]) + count_tokens_with_tiktoken(new_message["content"]) > token_limit:
            # Remove the oldest message
            history[user_id].popleft()

        # Add the new message
        history[user_id].append(new_message)
        
        # get_similar_faiss_id関数でmatched_idがあった場合は初期化
        if 'closest_titles' not in locals():
            closest_titles = []
            combined_scores = []
            closest_vector_indices = []
        
        full_response_content = ""
        log_data(cognito_user_id, local_log_option, user_message, response, full_response_content, actual_urls, closest_titles, combined_scores, closest_vector_indices, matched_ids)

        print(f"Conversation history for user {user_id}: {history[user_id]}")

    else:        
        # 必要な値をキャッシュに保存
        cache.set('user_id', user_id)
        cache.set(f"{user_id}_user_message", user_message)
        cache.set(f"{user_id}_history", list(history[user_id]))
        cache.set(f"{user_id}_token_limit", token_limit)
        cache.set(f"{user_id}_local_log_option", local_log_option)

        # actual_titlesとactual_urlsが存在する場合のみキャッシュにセット
        if actual_titles:
            cache.set(f"{user_id}_actual_titles", actual_titles)
        if actual_urls:
            cache.set(f"{user_id}_actual_urls", actual_urls)
            
        # closest_titlesが未定義の場合のみ初期化
        try:
            closest_titles
        except NameError:
            closest_titles = []

        # closest_titlesが存在する場合のみキャッシュにセット
        if closest_titles:
            cache.set(f"{user_id}_closest_titles", closest_titles)
            cache.set(f"{user_id}_combined_scores", combined_scores)
            cache.set(f"{user_id}_closest_vector_indices", closest_vector_indices)

        if matched_ids:
            cache.set(f"{user_id}_matched_ids", matched_ids)
            
        if cognito_user_id:
            cache.set(f"{user_id}_cognito_user_id", cognito_user_id)
            
        if local_model:
            cache.set(f"{user_id}_local_model", local_model)
            
        if local_history_maxlen:
            cache.set(f"{user_id}_local_history_maxlen", local_history_maxlen)
        
        if headers:
            cache.set(f"{user_id}_headers", headers)

        # フラグを含むJSONレスポンスを返す
        print("Sending ready_for_stream flag to frontend.")
        return jsonify({"ready_for_stream": True})

    response_data = {'message': new_message['content']}
    response = make_response(jsonify(response_data))
    response.headers["Content-Type"] = "application/json"
    return response


@app.route('/stream_response', methods=['GET'])
def stream_response():
    user_id = cache.get('user_id')
    user_message = cache.get(f"{user_id}_user_message")
    local_history_maxlen = cache.get(f"{user_id}_local_history_maxlen")
    user_history = deque(cache.get(f"{user_id}_history") or [], maxlen=local_history_maxlen)
    token_limit = cache.get(f"{user_id}_token_limit")
    local_log_option = cache.get(f"{user_id}_local_log_option")
    cognito_user_id = cache.get(f"{user_id}_cognito_user_id")
    local_model = cache.get(f"{user_id}_local_model")
    headers = cache.get(f"{user_id}_headers")

    # actual_titlesとactual_urlsがキャッシュに存在する場合に取得
    actual_titles = cache.get(f"{user_id}_actual_titles") or []
    actual_urls = cache.get(f"{user_id}_actual_urls") or []

    # closest_titles関連の値がキャッシュに存在する場合に取得
    closest_titles = cache.get(f"{user_id}_closest_titles") or []
    combined_scores = cache.get(f"{user_id}_combined_scores") or []
    closest_vector_indices = cache.get(f"{user_id}_closest_vector_indices")
    if closest_vector_indices is None or len(closest_vector_indices) == 0:
        closest_vector_indices = []

    # matched_idsがキャッシュに存在する場合に取得
    matched_ids = cache.get(f"{user_id}_matched_ids") or []

    def generate():
        print("generate function has started")
        try:
            print("Attempting to create a streaming response...")
            response_stream = openai.ChatCompletion.create(
                model=local_model,
                messages=list(user_history),
                headers=headers,
                stream=True
            )
            print("Streaming response created successfully.")
            
            full_response_content = ""  # AIからの完全なレスポンスを保存するための変数

            for chunk in response_stream:
                # print(f"Received chunk: {json.dumps(chunk, ensure_ascii=False, indent=2)}")
                if 'content' in chunk['choices'][0]['delta']:
                    content = chunk['choices'][0]['delta']['content']
                    full_response_content += content  # レスポンスを連結
                    
                    yield f"data: {json.dumps({'content': content})}\n\n"

            # AIのレスポンスに参照を追加する
            if actual_titles:
                references = ""
                for title, url in zip(actual_titles, actual_urls):
                    if validators.url(url):
                        references += f'<br><br><a href="{url}" target="_blank">{title}</a>'
                    else:
                        references += f'<br><br>{title} ({url})'
                new_message = {"role": "assistant", "content": full_response_content + references}
                yield f"data: {json.dumps({'content': references})}\n\n"
            else:
                new_message = {"role": "assistant", "content": full_response_content}

            # トリム後のトークン数を確認
            new_message_tokens = count_tokens_with_tiktoken(new_message["content"])
            print(f"Tokens in final new_message (after AI response): {new_message_tokens}")

            # Check if the new message would cause the total tokens to exceed the limit
            while sum(count_tokens_with_tiktoken(message["content"]) for message in user_history) + new_message_tokens > token_limit:
                # Remove the oldest message from user_history
                user_history.pop(0)

            # Add the new message to user_history
            user_history.append(new_message)
            
            response = ""
            log_data(cognito_user_id, local_log_option, user_message, response, full_response_content, actual_urls, closest_titles, combined_scores, closest_vector_indices, matched_ids)
            
            # Update the global history with the user's updated history
            history[user_id] = user_history

            print(f"Conversation history for user {user_id}: {history[user_id]}")

        except ServiceUnavailableError:
            yield f"data: {json.dumps({'error': 'The server is overloaded or not ready yet. Please try again later.'})}\n\n"
        except Exception as e:
            print(f"Error while generating chat completion: {e}")
            yield f"data: {json.dumps({'error': 'Failed to generate a response.'})}\n\n"

    # 関数の最後でキャッシュを削除
    if cache.get('user_id'):
        cache.delete('user_id')
    if cache.get(f"{user_id}_user_message"):
        cache.delete(f"{user_id}_user_message")
    if cache.get(f"{user_id}_history"):
        cache.delete(f"{user_id}_history")
    if cache.get(f"{user_id}_actual_titles"):
        cache.delete(f"{user_id}_actual_titles")
    if cache.get(f"{user_id}_actual_urls"):
        cache.delete(f"{user_id}_actual_urls")
    if cache.get(f"{user_id}_token_limit"):
        cache.delete(f"{user_id}_token_limit")
    if cache.get(f"{user_id}_local_log_option"):
        cache.delete(f"{user_id}_local_log_option")
    if cache.get(f"{user_id}_cognito_user_id"):
        cache.delete(f"{user_id}_cognito_user_id")
    if cache.get(f"{user_id}_local_model"):
        cache.delete(f"{user_id}_local_model")
    if cache.get(f"{user_id}_local_history_maxlen"):
        cache.delete(f"{user_id}_local_history_maxlen")
    if cache.get(f"{user_id}_headers"):
        cache.delete(f"{user_id}_headers")

    if cache.get(f"{user_id}_closest_titles"):  # closest_titlesがキャッシュに存在する場合のみ削除
        cache.delete(f"{user_id}_closest_titles")
        cache.delete(f"{user_id}_combined_scores")
        cache.delete(f"{user_id}_closest_vector_indices")

    if cache.get(f"{user_id}_matched_ids"):  # matched_idsがキャッシュに存在する場合のみ削除
        cache.delete(f"{user_id}_matched_ids")

    try:
        response = Response(stream_with_context(generate()), content_type='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        print(f"Error when calling generate function: {e}")


def log_data(cognito_user_id, local_log_option, user_message, response, full_response_content, actual_urls, closest_titles, combined_scores, closest_vector_indices, matched_ids):
    print(f"logdata has started with log_option: {local_log_option} and matched_ids: {matched_ids}")
    
    if local_log_option == 'fine_tune':
        try:
            # Check if the file is empty (i.e., if it's a new file)
            log_data_path = os.path.join(os.path.dirname(__file__), f"dir_{cognito_user_id}", 'fine_tuning_data.csv')
            is_new_file = not os.path.exists(log_data_path) or os.stat(log_data_path).st_size == 0

            # Log the user messages and AI responses to a CSV file
            with open(log_data_path, 'a', newline='') as file:
                fieldnames = ['prompt', 'completion']
                writer = csv.DictWriter(file, fieldnames=fieldnames)

                # Write the header only if the file is new
                if is_new_file:
                    writer.writeheader()

                if 'choices' in response:  # responseがstreamでない場合
                    ai_response_content = response['choices'][0]['message']['content']
                else:  # responseがstreamの場合
                    ai_response_content = full_response_content
     
                writer.writerow({
                    'prompt': user_message,
                    'completion': ai_response_content
                })
        except Exception as e:
            print(f"Error while logging in 'fine_tune' option: {e}")

    elif local_log_option == 'vector_score_log' and not matched_ids:
        try:
            # Check if the file is empty (i.e., if it's a new file)
            log_data_path = os.path.join(os.path.dirname(__file__), f"dir_{cognito_user_id}", 'vector_score.csv')
            is_new_file = not os.path.exists(log_data_path) or os.stat(log_data_path).st_size == 0

            # Log the distances and URLs etc... to a CSV file
            with open(log_data_path, 'a', newline='') as file:
                fieldnames = ['user_message', 'title', 'id', 'score', 'actual_referred_urls', 'ai_response']
                writer = csv.DictWriter(file, fieldnames=fieldnames)

                # Write the header only if the file is new
                if is_new_file:
                    writer.writeheader()

                # Ensure all lists have the same length
                while len(actual_urls) < len(closest_titles):
                    actual_urls.append(None)

                adjusted_ids = [idx + 2 for idx in closest_vector_indices]
                for title, each_score, each_url, adj_id in zip(closest_titles, combined_scores, actual_urls, adjusted_ids):
                    
                    if 'choices' in response:  # responseがstreamでない場合
                        ai_response_content = response['choices'][0]['message']['content']
                    else:  # responseがstreamの場合
                        ai_response_content = full_response_content
                    writer.writerow({
                        'user_message': user_message, 
                        'title': title,
                        'id': adj_id,
                        'score': each_score,
                        'actual_referred_urls': each_url,
                        'ai_response': ai_response_content
                    })
                
                print("Finished writing to file.")
        except Exception as e:
            print(f"Error while logging in 'vector_score_log' option: {e}")


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True, threaded=True)