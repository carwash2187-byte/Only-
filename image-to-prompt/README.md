# Image → Prompt

A one-screen tool: drop in an image, get back a 3-5 sentence prompt you can paste
into an AI image generator (Midjourney, Stable Diffusion, DALL·E) or video
generator (Runway, Sora, Kling, Pika).

It's a small Node/Express server with a plain HTML/CSS/JS frontend. The image
never leaves your own server except to go to OpenAI's vision API for analysis.

## Setup

```bash
cd image-to-prompt
npm install
cp .env.example .env
```

Edit `.env` and add your OpenAI API key:

```
OPENAI_API_KEY=sk-...
```

Get a key from https://platform.openai.com/api-keys. The key is only ever used
server-side — it's never sent to the browser.

## Run

```bash
npm start
```

Open http://localhost:3000, choose or drag in an image, click **Generate
Prompt**.

## Configuration

Everything is optional except the API key:

| Variable       | Default  | Purpose                                      |
|----------------|----------|-----------------------------------------------|
| `OPENAI_API_KEY` | —      | Required. Your OpenAI API key.                |
| `OPENAI_MODEL`   | `gpt-4o` | Vision-capable model to use. Change this if you have access to a newer model on your account. |
| `PORT`           | `3000`   | Port the server listens on.                   |

## Using a different provider

The API call lives in one place: the `/api/generate-prompt` route in
`server.js`. To swap in Anthropic, Gemini, or another vision-capable model,
replace the `fetch('https://api.openai.com/...')` call there with the
equivalent request for that provider.
