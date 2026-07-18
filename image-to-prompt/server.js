require('dotenv').config();
const express = require('express');
const multer = require('multer');
const path = require('path');

const PORT = process.env.PORT || 3000;
const OPENAI_API_KEY = process.env.OPENAI_API_KEY;
const OPENAI_MODEL = process.env.OPENAI_MODEL || 'gpt-4o';

// Written so the output works equally well pasted into an image generator
// (Midjourney, Stable Diffusion) or a video generator (Runway, Sora, Kling).
const PROMPT_INSTRUCTIONS = `You turn images into prompts for AI image and video generators.
Describe the attached image as a single vivid paragraph of 3-5 sentences (roughly 60-120 words)
that could be pasted directly into a tool like Midjourney, Stable Diffusion, Runway, or Sora to
recreate a similar image or animate it. Cover the main subject, setting, composition, lighting,
color palette, mood, and artistic style. Write it as flowing descriptive prose, not a list or
bullet points. Output only the prompt text itself - no preamble, quotation marks, or labels.`;

const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => {
    if (!file.mimetype.startsWith('image/')) {
      return cb(new Error('Only image files are allowed.'));
    }
    cb(null, true);
  },
});

const app = express();
app.use(express.static(path.join(__dirname, 'public')));

app.post('/api/generate-prompt', (req, res) => {
  upload.single('image')(req, res, async (uploadErr) => {
    if (uploadErr) {
      return res.status(400).json({ error: uploadErr.message });
    }
    if (!req.file) {
      return res.status(400).json({ error: 'No image was uploaded.' });
    }
    if (!OPENAI_API_KEY) {
      return res.status(500).json({
        error: 'Server is missing OPENAI_API_KEY. Add it to image-to-prompt/.env and restart the server.',
      });
    }

    const dataUrl = `data:${req.file.mimetype};base64,${req.file.buffer.toString('base64')}`;

    try {
      const response = await fetch('https://api.openai.com/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${OPENAI_API_KEY}`,
        },
        body: JSON.stringify({
          model: OPENAI_MODEL,
          messages: [
            {
              role: 'user',
              content: [
                { type: 'text', text: PROMPT_INSTRUCTIONS },
                { type: 'image_url', image_url: { url: dataUrl } },
              ],
            },
          ],
          max_tokens: 300,
          temperature: 0.7,
        }),
      });

      const data = await response.json();

      if (!response.ok) {
        const message = data?.error?.message || `OpenAI API request failed (${response.status}).`;
        return res.status(502).json({ error: message });
      }

      const prompt = data.choices?.[0]?.message?.content?.trim();
      if (!prompt) {
        return res.status(502).json({ error: 'OpenAI returned an empty response.' });
      }

      res.json({ prompt });
    } catch (err) {
      res.status(500).json({ error: `Failed to reach OpenAI: ${err.message}` });
    }
  });
});

app.listen(PORT, () => {
  console.log(`Image -> Prompt running at http://localhost:${PORT}`);
});
