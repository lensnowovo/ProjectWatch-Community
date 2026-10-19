/** Submit a selected batch once, preserving order and isolating per-file errors. */
export async function uploadBatch(files, uploadOne, onProgress) {
  const result = {
    total: files.length,
    attempted: 0,
    accepted: 0,
    failures: [],
    notSent: 0,
    stopped: false,
  };
  const publish = () => onProgress({ ...result, failures: [...result.failures] });
  publish();
  for (const file of files) {
    try {
      await uploadOne(file);
      result.accepted += 1;
    } catch (error) {
      const reason = typeof error?.message === "string" && error.message.trim() ? error.message : "上传失败";
      result.failures.push({ name: file.name, reason });
      // Authentication, server, and transport failures are likely batch-wide.
      if (!error?.status || error.status === 401 || error.status === 403 || error.status >= 500) {
        result.stopped = true;
      }
    }
    result.attempted += 1;
    result.notSent = result.total - result.attempted;
    publish();
    if (result.stopped) break;
  }
  return result;
}
