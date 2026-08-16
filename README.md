# StudyGraph

StudyGraph is a local Flask app that turns uploaded study materials into a structured topic graph. It extracts smaller, teachable concepts from files, connects related ideas, and lets students ask questions using selected graph topics.

## What it does

- Upload study materials and generate a study graph.
- Break large lectures into focused topics instead of one oversized summary.
- Store topics, lecture metadata, file metadata, difficulty, teaching order, and connections.
- Ask AI questions about selected topics using only the graph context.
- Use a local PostgreSQL database for app data.

## Requirements

Before running the app, install:

- Windows 10 or newer
- Python 3.10+
- PostgreSQL
- An OpenAI API key — I have included my API key, and it is valid until the end of summer.

The Windows launcher creates a virtual environment, installs Python dependencies from `requirements.txt`, and uses `studygraph_setup_env.py` to create or update the `.env` file automatically.

### macOS and Linux note

This project should also work on macOS and Linux because the app itself is written in Python and uses PostgreSQL. However, the included `run_app.bat` launcher is only for Windows.

I have not included an equivalent macOS/Linux shell script because I originally built the setup automation as a Windows batch file. macOS and Linux users can still run the project manually by installing the requirements, updating the .env file, and starting the app from the terminal.

## PostgreSQL setup

1. Download and install PostgreSQL for Windows.
2. During installation, write down:
   - the password for the `postgres` user
   - the port number, usually `5432`
3. Open pgAdmin after installation.
4. Create a database named `studygraph`, or use a different name and enter it during setup.

## Environment variables

The app uses a `.env` file in the project folder. If one does not exist, the launcher creates it.

Required values:

```env
FLASK_SECRET_KEY=generated_automatically
OPENAI_API_KEY=your_openai_api_key_here
DATABASE_URL=postgresql+psycopg://postgres:your_password@localhost:5432/studygraph
```

If `FLASK_SECRET_KEY` is missing, the setup script generates one automatically.

Optional model overrides:

```env
OPENAI_GRAPH_MODEL=gpt-5.4-mini
OPENAI_ASK_MODEL=gpt-5.4-nano
```
These models are recommended because higher-tier models may use tokens quickly, while lower-tier models may produce less reliable or lower-quality results.

## Running on Windows

Double-click:

```text
run_app.bat
```

The script will:

1. Create `.venv` if it does not already exist.
2. Install dependencies from `requirements.txt`.
3. Run `studygraph_setup_env.py`.
4. Create `.env` if needed.
5. Generate `FLASK_SECRET_KEY` automatically if it is missing.
6. Ask for an OpenAI API key if it is missing.
7. Ask for PostgreSQL settings if `DATABASE_URL` is missing.
8. Test the PostgreSQL connection.
9. Save the values to `.env`.
10. Start the app with `app.py`.

## Database setup prompts

When prompted, most users can use these defaults:

```text
Host: localhost
Port: 5432
Username: postgres
Database name: studygraph
```

Use different values only if your PostgreSQL installation uses a custom host, port, username, or database name.

If your password contains special URL characters such as `@`, `:`, `/`, `?`, `#`, `%`, or `&`, the improved launcher safely encodes the username and password before creating `DATABASE_URL`.

## Manual database URL

If automatic setup does not work, paste a full database URL when asked:

```text
postgresql+psycopg://postgres:password@localhost:5432/studygraph
```

If your password contains special characters, URL-encode it first. For example, `p@ss word` becomes `p%40ss+word`.

## Common problems

### Could not connect to PostgreSQL

Check that:

- PostgreSQL is installed.
- The PostgreSQL server is running.
- The port is correct.
- The username and password are correct.
- The database exists in pgAdmin.

### `requirements.txt` install failed

Try running this manually from the project folder:

```bat
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### No OpenAI API key is set

Open `.env` and add:

```env
OPENAI_API_KEY=your_openai_api_key_here
```

## Project notes

StudyGraph expects generated graph fragments to be valid JSON with lectures, topics, lecture metadata, and file metadata. Topics are designed to be small, readable study units with sparse, useful connections instead of dense clutter.


## Contributions

- **Nakul (ngardesh)**  
  Implemented the full application, including the backend, frontend, database setup, AI integration, testing, and created demo video.

- **ChatGPT**  
  Assisted with parts of the AI integration and helped with the HTML, CSS, and JavaScript used in the frontend.

- **Rishi (rsivaku4)**  
  Created the project presentation and helped black-box test the application.

- **Emerson (emmill23)**  
  Created the AI breakdown document and helped black-box test the application.

## Project structure

```text
.
├── static/
├── templates/
├── uploads/
├── .env.example
├── app.py
├── contributions.txt
├── README.md
├── requirements.txt
├── run_app.bat
├── studygraph_ai.py
└── studygraph_setup_env.py
```

## Future Improvements

There are several areas where StudyGraph could be improved in the future:

- **Improve account security**  
  The signup and login system could be tightened up by adding stronger validation, email-based two-factor authentication, and checks to make sure a provided OpenAI API key is valid before saving or using it.

- **Improve quiz answer choices**  
  Currently, quiz answer choices can come from summaries of random topics. A better approach would be to generate incorrect choices from related graph nodes, topics from the same lecture, or semantically similar topics. This would make the quiz options more realistic and challenging.

- **Preprocess uploaded materials before sending them to the AI**  
  Right now, full uploaded files are sent for processing. In the future, the backend could strip out unrelated or unnecessary content before sending files to the AI. This would allow larger batches of materials to be processed and could also reduce API costs.

- **Remove file reprocessing from the library page**  
  The library page currently includes an option to reprocess uploaded files. Removing this feature would simplify the backend because the app would no longer need to keep the original uploaded files after they have already been processed.