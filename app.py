from flask import Flask, request, jsonify, make_response, render_template, redirect, url_for, stream_with_context, Response
from flask_httpauth import HTTPBasicAuth
from flask_caching import Cache
from flask_cors import CORS
import openai
from collections import deque
import numpy as np
import json
from datetime import datetime, timedelta
from langdetect import detect
import os
from sklearn.metrics.pairwise import cosine_similarity
import threading
from typing import Dict, Any, Deque
from src.schema.logging_config import logger
from src.schema.config_manager import load_settings, settings_lock, user_settings
from src.functions.stream_response import generate
from src.functions.get_similar_faiss_id import get_similar_faiss_id
from src.functions.embedding import embedding_user_message
from src.functions.unstreamed_response import generate_chat_response, process_chat_response
from src.utils.cache_utils import check_cache_expiry, cognito_cache
from src.utils.cognito_utils import set_cognito_data
from src.utils.user_cache import set_user_cache, get_user_cache
from src.utils.token_utils import count_tokens_with_tiktoken, trim_to_tokens, get_token_limit
from src.utils.file_utils import update_settings_path

openai.api_key = ""

app = Flask(__name__)
cache = Cache(app, config={'CACHE_TYPE': 'simple'})
CORS(app)

# スレッドローカル変数の定義
local_data = threading.local()

# スレッドを起動
cache_thread = threading.Thread(target=check_cache_expiry)
cache_thread.daemon = True  # メインプログラムが終了したらスレッドも終了
cache_thread.start()

@app.route('/get_cognito_id', methods=['POST'])
def get_cognito_id_route():
    try:
        cognito_user_id = request.form.get("member_id")
        result = set_cognito_data(cognito_user_id)
        if result is None:
            logger.warning("Failed to set cognito data")
            return "Failed to set cognito data", 500
        return cognito_user_id
    except Exception as e:
        logger.error(f"Error in get_cognito_id_route: {e}")
        return "Internal Server Error", 500

auth = HTTPBasicAuth()

@auth.verify_password
def verify_password(username, password):
    cognito_user_id = username
    settings = load_settings(cognito_user_id)
    
    USERNAME = settings['USERNAME']
    PASSWORD = settings['PASSWORD']
    
    if USERNAME is None or PASSWORD is None:
        # 新規ユーザーの場合、パスワード設定プロセスを開始
        return start_password_setup(cognito_user_id, password)
    
    return cognito_user_id == USERNAME and password == PASSWORD

def start_password_setup(cognito_user_id, password):
    # パスワード設定プロセスを実装
    # 例: 一時的なトークンを生成し、パスワード設定ページにリダイレクト
    pass

@app.route('/config/<string:cognito_user_id>', methods=['GET'])
@auth.login_required
def config(cognito_user_id):
    settings = load_settings(cognito_user_id)
    
    # MAX_REQUESTSの小数点を省く
    if isinstance(settings['max_requests'], (int, float)) and settings['max_requests'] != float('inf'):
        settings['max_requests'] = str(int(settings['max_requests']))
    else:
        settings['max_requests'] = 'inf'
    
    # Zip the questions and corresponding_ids
    zipped_questions_ids = list(zip(settings['questions'], settings['corresponding_ids']))

    # Pass the zipped list to the template
    return render_template('config.html', cognito_user_id=cognito_user_id, zipped_questions_ids=zipped_questions_ids, **settings)

@app.route('/save_config/<string:cognito_user_id>', methods=['POST'])
def save_config(cognito_user_id):
    settings_path = update_settings_path(cognito_user_id)
    # local variables
    api_key = request.form.get('api_key')
    max_requests_str = request.form.get('max_requests')
    max_requests = float(max_requests_str) if max_requests_str and max_requests_str.strip() != '' else float('inf')
    reset_time = int(request.form.get('reset_time', 0))
    threshold_str = request.form.get('threshold')
    threshold = float(threshold_str) if threshold_str is not None else None
    model = request.form.get('model')
    knowledge_about_user = request.form.get('knowledge_about_user')
    response_preference = request.form.get('response_preference')
    log_option = request.form.get('log_option')
    history_maxlen_value = request.form.get('history_maxlen')
    history_maxlen = int(history_maxlen_value) if history_maxlen_value and history_maxlen_value.strip() != '' else float('inf')
    username = request.form.get('USERNAME')
    password = request.form.get('PASSWORD')
    questions = request.form.getlist('questions[]')
    corresponding_ids = request.form.getlist('corresponding_ids[]')

    new_settings = {
        'api_key': api_key,
        'max_requests': "Infinity" if max_requests == float('inf') else max_requests,
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

    with settings_lock:  # ロックをかける
        # Update user_settings dictionary
        user_settings[cognito_user_id] = new_settings

        # Save the settings to a JSON file
        with open(settings_path, 'w', encoding="utf-8") as f:
            json.dump(new_settings, f, ensure_ascii=False)

    # Redirect to the configuration page
    return redirect(url_for('config', cognito_user_id=cognito_user_id))

# Initialize history and last active time as dictionaries
history: Dict[str, Deque[Dict[str, str]]] = {}
last_active: Dict[str, Any] = {}

# ユーザーIDとリクエスト数を保存するパラメーター
user_requests: Dict[str, Dict[str, Any]] = {}

@app.route('/message', methods=['POST'])
def message():
    data = request.get_json()
    user_message = data['message']['text']
    user_id = str(data.get("user_id", ""))
    session_id = data.get("session_id")
    cognito_user_id = data.get("member_id")
    stream = data.get("stream", False)

    # ここら辺の変数は初期化しないとエラーが出る時がある
    actual_titles = []
    actual_urls = []
    closest_titles = []
    combined_scores = []
    closest_vector_indices = []
    matched_ids = []
    
    # キャッシュから値を取得
    cached_data = cognito_cache.get(cognito_user_id, {})
    
    if not cached_data:
        # キャッシュにデータがない場合、set_cognito_dataを呼び出す
        if not set_cognito_data(cognito_user_id):
            return jsonify({"error": "Failed to set cognito data"}), 500
        cached_data = cognito_cache.get(cognito_user_id, {})
    
    settings = cached_data.get('settings', {})
    file_path = cached_data.get('file_path')
    data = cached_data.get('data')
    documents = cached_data.get('documents')
    vectors_path = cached_data.get('vectors_path')
    vectors = cached_data.get('vectors')
    index = cached_data.get('index')
    
    # 設定値を取得
    local_api_key = settings.get('api_key')
    headers = {"Authorization": f"Bearer {local_api_key}"}
    local_max_requests = settings.get('max_requests')
    local_reset_time = settings.get('reset_time')
    local_threshold = settings.get('threshold')
    local_model = settings.get('model')
    local_knowledge_about_user = settings.get('knowledge_about_user')
    local_response_preference = settings.get('response_preference')
    local_log_option = settings.get('log_option')
    local_history_maxlen = settings.get('history_maxlen')
    local_questions = settings.get('questions')
    local_corresponding_ids = settings.get('corresponding_ids')
    
    logger.info(f"Cognito User ID Check: {cognito_user_id}")
    logger.info(f"reference.json path: {file_path}")
    logger.info(f"vectors.npy path: {vectors_path}")
    
    # Initialize the history for this user or session if it doesn't exist
    if user_id not in history:
        if local_history_maxlen == float('inf'):
            history[user_id] = deque()
        else:
            history[user_id] = deque(maxlen=local_history_maxlen)
        last_active[user_id] = datetime.now()
    else:
        logger.info(f"Current history length for user {user_id}: {len(history[user_id])}")
    
    # Print the user ID and session ID
    logger.info(f"User ID: {user_id}")
    logger.info(f"Session ID: {session_id}")
    logger.info(f"User message from user {user_id}: {user_message}") 

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
    
    # ユーザーメッセージの言語を検出
    language = detect(user_message)
    
    # メッセージのプレフィックスを設定
    if language == "ja":
        prefix = "データベースによると:"
    else:
        prefix = "According to our database:"
        
    # questionsとcorresponding_idsを組み合わせてプロンプトを作成
    combined_list = "\n".join([f"{q} - ID: {id_}" for q, id_ in zip(local_questions, local_corresponding_ids)])
        
    # combined_listが空でない場合のみget_similar_faiss_id関数を呼び出す
    if combined_list:
        # get_similar_faiss_id関数を呼び出す
        matched_ids = get_similar_faiss_id(headers, local_model, local_knowledge_about_user, user_message, user_id, history, prefix, combined_list)
        logger.info(f"Returned Matched IDs: {matched_ids}")
    else:
        logger.info("No questions and corresponding IDs provided. Skipping the Q&IDs process.")
        
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
        user_message_vector = embedding_user_message(user_message, headers)
        if user_message_vector is None:  # エラーレスポンスが返された場合
            return jsonify({"error": "Failed to process user message."}), 500

        # Query the index with the user's message vector
        try:
            D, I = index.search(np.array([user_message_vector], dtype=np.float32), 10)
        except Exception as e:
            logger.error(f"Error while searching the index: {e}")
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
        logger.info(f"Closest document FAISS IDs (adjusted for CSV): {[idx + 2 for idx in closest_vector_indices]}")
        logger.info(f"Closest document titles: {closest_titles}")
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
                logger.warning(f"Document not found in reference list: {title}")
                similarities.append(0)

        # 距離スコアとコサイン類似性のスコアを組み合わせて新しいスコアを計算
        combined_scores = [0.5 * (1 - scaled_distance) + 0.5 * similarity for scaled_distance, similarity in zip(closest_vector_distances, similarities)]
        logger.info(f"Combined scores (Scaled FAISS distance + Cosine similarity): {combined_scores}")

        # Thresholdを超えたIDを履歴に追加
        # スコアと関連する情報を同時にソート
        sorted_indices = np.argsort(combined_scores)[::-1]  # スコアを降順でソート
        added_count = 0

        for idx in sorted_indices:
            score = combined_scores[idx]
            if score >= local_threshold:
                title = closest_titles[idx]
                url = closest_urls[idx]
                doc = closest_documents[idx]
                
                actual_titles.append(title)
                actual_urls.append(url)
                
                logger.info(f"Actual Referred titles: {title}")
                logger.info(f"Actual Referred urls: {url}")
                
                document_content = f"{title} {doc})"
                reference_content = f"{prefix} {document_content}"

                # temp_referenceに追加（後にhistory.appendされる）
                temp_references.append({"role": "assistant", "content": reference_content})
                
                added_count += 1
                if added_count >= 4:  # 上位4つの結果を保存
                    break

    # 一時的なリストを逆順にして、historyに追加
    for reference in reversed(temp_references):
        history[user_id].append(reference)
 
    # Calculate the total tokens for messages in history[user_id]
    total_tokens = sum(count_tokens_with_tiktoken(message["content"]) for message in history[user_id])
    logger.info(f"Total tokens: {total_tokens}")
    
    # モデルに基づいてトークン制限を設定
    token_limit = get_token_limit(local_model)
    logger.info(f"選択されたモデルとそのトークン制限: {local_model, token_limit}")

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
        response, error_response = generate_chat_response(local_model, history, user_id, headers)
        if error_response:
            return error_response

        new_message = process_chat_response(response, actual_titles, actual_urls, history, user_id, token_limit, local_log_option, user_message, cognito_user_id, matched_ids)

    else:        
        logger.info("Entering streaming mode")
        set_user_cache(
            cache, user_id, user_message, history[user_id], token_limit, local_log_option,
            actual_titles, actual_urls, closest_titles, combined_scores, closest_vector_indices,
            matched_ids, cognito_user_id, local_model, local_history_maxlen, headers
        )

        # フラグを含むJSONレスポンスを返す
        logger.info("Sending ready_for_stream flag to frontend.")
        return jsonify({"ready_for_stream": True})

    response_data = {'message': new_message['content']}
    response = make_response(jsonify(response_data))
    response.headers["Content-Type"] = "application/json"
    return response


@app.route('/stream_response', methods=['GET'])
def stream_response():
    user_id = str(cache.get('user_id') or "")
    cache_data = get_user_cache(cache, user_id)

    # キャッシュを削除
    # clear_user_cache(cache, user_id)

    try:
        response = Response(stream_with_context(generate(**cache_data, history=history)), content_type='text/event-stream')
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except Exception as e:
        logger.error(f"Error when calling generate function: {e}")


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)), debug=True, threaded=True)