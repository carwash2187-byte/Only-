const dropzone = document.getElementById('dropzone');
const dropzoneHint = document.getElementById('dropzone-hint');
const fileInput = document.getElementById('file-input');
const preview = document.getElementById('preview');
const generateBtn = document.getElementById('generate-btn');
const statusEl = document.getElementById('status');
const resultBox = document.getElementById('result-box');
const resultText = document.getElementById('result-text');
const copyBtn = document.getElementById('copy-btn');

let selectedFile = null;

function setStatus(message, isError) {
  if (!message) {
    statusEl.hidden = true;
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.classList.toggle('error', Boolean(isError));
}

function selectFile(file) {
  if (!file || !file.type.startsWith('image/')) {
    setStatus('Please choose an image file.', true);
    return;
  }
  selectedFile = file;
  preview.src = URL.createObjectURL(file);
  preview.hidden = false;
  dropzoneHint.hidden = true;
  generateBtn.disabled = false;
  resultBox.hidden = true;
  setStatus('');
}

dropzone.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', () => {
  selectFile(fileInput.files[0]);
});

['dragenter', 'dragover'].forEach((eventName) => {
  dropzone.addEventListener(eventName, (e) => {
    e.preventDefault();
    dropzone.classList.add('dragover');
  });
});

['dragleave', 'drop'].forEach((eventName) => {
  dropzone.addEventListener(eventName, (e) => {
    e.preventDefault();
    dropzone.classList.remove('dragover');
  });
});

dropzone.addEventListener('drop', (e) => {
  const file = e.dataTransfer.files[0];
  selectFile(file);
});

generateBtn.addEventListener('click', async () => {
  if (!selectedFile) return;

  generateBtn.disabled = true;
  resultBox.hidden = true;
  setStatus('Generating prompt...');

  const formData = new FormData();
  formData.append('image', selectedFile);

  try {
    const response = await fetch('/api/generate-prompt', {
      method: 'POST',
      body: formData,
    });
    const data = await response.json();

    if (!response.ok) {
      throw new Error(data.error || 'Something went wrong.');
    }

    resultText.textContent = data.prompt;
    resultBox.hidden = false;
    setStatus('');
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    generateBtn.disabled = false;
  }
});

copyBtn.addEventListener('click', async () => {
  await navigator.clipboard.writeText(resultText.textContent);
  const original = copyBtn.textContent;
  copyBtn.textContent = 'Copied!';
  setTimeout(() => {
    copyBtn.textContent = original;
  }, 1500);
});
