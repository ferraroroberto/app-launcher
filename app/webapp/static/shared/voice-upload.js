/* The single-shot dictation take as the form body /api/transcribe receives. */

// The PCM capture's WAV, or null when no capture ran or it heard nothing.
async function pcmWav(pcmStart) {
  if (!pcmStart) return null;
  try {
    const capture = await pcmStart;
    return capture ? await capture.stop() : null;
  } catch (exc) {
    console.info('[dictation] PCM capture did not finish', (exc && exc.message) || exc);
    return null;
  }
}

// The take as the upload's form body: the 16 kHz mono WAV recorded straight
// from the microphone (no container is ever converted, so the PC needs no
// ffmpeg and iPhone Safari works), or, when PCM capture was unavailable or
// heard nothing, the browser's own recording, which the server converts.
export async function uploadForm(blob, pcmStart) {
  const fd = new FormData();
  const wav = await pcmWav(pcmStart);
  if (wav) {
    console.info('[dictation] uploading the take as WAV (pcm)');
    fd.append('file', wav, 'recording.wav');
    return fd;
  }
  const ext = (blob.type && blob.type.indexOf('mp4') >= 0) ? 'mp4' : 'webm';
  console.info('[dictation] uploading the browser recording as ' + ext + ' (raw): PCM capture gave no WAV');
  fd.append('file', blob, 'recording.' + ext);
  return fd;
}
