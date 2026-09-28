export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

export async function readImageInput(file: File): Promise<string> {
  if (!IMAGE_TYPES.has(file.type)) {
    throw new Error('Choose a PNG, JPEG, or WebP image.');
  }
  if (file.size === 0 || file.size > MAX_IMAGE_BYTES) {
    throw new Error('Image must be smaller than 10 MiB.');
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('Could not read image.'));
    reader.readAsDataURL(file);
  });
  const encoded = dataUrl.split(',', 2)[1];
  if (!encoded) throw new Error('Could not read image.');
  return encoded;
}
