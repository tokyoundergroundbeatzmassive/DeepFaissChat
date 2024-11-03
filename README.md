# DeepFaissChat
---

***This package consists of three programs.***

1. **EMBED-IT**: Extracts and embeds text from URLs or sitemaps. Text from PDFs and CSVs can also be used. This process generates "vectors.npy," which stores the vector representations of the text, and "reference.json," which stores the corresponding reference text.
   - [View Code](hhttps://github.com/tokyoundergroundbeatzmassive/EMBED_IT)
   - Clone: `git clone https://github.com/tokyoundergroundbeatzmassive/EMBED_IT.git`

2. **DeepFaissChat**: Backend for generating Chat Bot responses.
   1. In the demo version, manually create a `dir_{member_id}` folder at the same level as `app.py` (the `member_id` should match the Member ID set in **Me_Cool_Custom_Line_Bot**).
   2. Place the "vectors.npy" and "reference.json" generated by **EMBED-IT** into the folder created above.
   3. Configure OpenAI API keys and other settings at `/config/member_id` (Initial HTTP Basic Authentication is admin/password).
   - **This Repository is the one.**  

3. **Me_Cool_Custom_Line_Bot**: Client-side WP Plugin.
   1. Set the `member_id` from the `dir_{member_id}` created in **DeepFaissChat** as the Member ID.
   2. The APP URL is `http://localhost:8080/` when testing locally.
   3. Access **DeepFaissChat**'s `/config/member_id` from the "Backend Config" button.
   - [View Code](https://github.com/tokyoundergroundbeatzmassive/Me_Cool_Custom_Line_Bot)
   - Clone: `git clone https://github.com/tokyoundergroundbeatzmassive/Me_Cool_Custom_Line_Bot.git`

---

## How to Use

- Create a Python 3 virtual environment within the project folder.
- From the virtual environment, run the `pip install -r requirements.txt` command to install the required libraries.
- From the virtual environment, execute `python app.py` or `nohup python app.py &` to launch the application.

### Note: Server Sent Event Support is Required
This application utilizes Server Sent Events and will only function in environments that support this technology. If you are using an instance like EC2, please configure an Apache server and set up a reverse proxy on port 8080.

### Local Testing
For local testing, you can launch the WP plugin client using MAMP, making it possible to test in a local environment.