# Deploy Sanjeevani AI

## Recommended: Render

1. Push this project folder to GitHub.
2. On Render, create a new Web Service from the GitHub repo.
3. Use:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. Add environment variables:
   - `GROQ_API_KEY`
   - `SECRET_KEY`
   - `WTF_CSRF_ENABLED=false`
   - `GROQ_MODEL=llama-3.1-8b-instant`
5. Deploy and open the generated `.onrender.com` URL.

## Railway

1. Create a Railway project from the GitHub repo.
2. Set Start Command to `gunicorn app:app` if Railway does not detect it.
3. Add the same environment variables listed above.
4. Generate a public domain from the service settings.

## Important

Never commit `.env` or hardcoded API keys. If a key was committed or shared,
rotate it in the Groq dashboard before going live.
