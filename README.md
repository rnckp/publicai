# Public AI Challenge - Team 34

## Test the OpenAI API

Install the project dependencies:

```sh
uv sync
```

If `.env` does not exist yet, copy `.env.example` to `.env`. Set `OPENAI_API_KEY` in `.env`, then run the CLI from the repository root:

```sh
uv run python scripts/test_openai.py
```

To send a different prompt:

```sh
uv run python scripts/test_openai.py "Say hello in German."
```

The default prompt asks for the capital of Switzerland. The script sends one request to `gpt-6-luna` with low reasoning effort and prints the response.

## Problem

- 2110 municipalities in Switzerland
- Almost every municipalitys website and online services have different shapes

## Solution

- ...
