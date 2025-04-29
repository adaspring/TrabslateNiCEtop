import os
import json
import re
import requests
import logging
from bs4 import BeautifulSoup
import argparse
import sys

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Config
TRANSLATION_MEMORY = "translation_db.json"
INJECT_HEAD = [
    '<meta name="robots" content="index, follow">',
    '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
]
INJECT_BODY = [
    '<script src="/scripts/custom.js"></script>',
    '<script>console.log("Translated version loaded")</script>'
]

LANG_MAP = {
    "fr": {"name": "French", "deepl": "FR"},
    "es": {"name": "Spanish", "deepl": "ES"},
    "de": {"name": "German", "deepl": "DE"}
}

LIBRE_SERVERS = [
    "https://translate.argosopentech.com",
    "https://libretranslate.de",
    "https://libretranslate.com"
]

# Get API keys from environment
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Check if API keys are available and import openai if needed
has_openai = False
if OPENAI_API_KEY:
    try:
        import openai
        openai.api_key = OPENAI_API_KEY
        has_openai = True
    except ImportError:
        logger.warning("OpenAI package not found. GPT refinement disabled.")

# Load/save memory
def load_memory():
    if os.path.exists(TRANSLATION_MEMORY):
        try:
            with open(TRANSLATION_MEMORY, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logger.error(f"Error parsing {TRANSLATION_MEMORY}. Creating new memory.")
            return {}
    return {}

def save_memory(memory):
    try:
        with open(TRANSLATION_MEMORY, 'w', encoding='utf-8') as f:
            json.dump(memory, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save translation memory: {e}")

# Translation functions
def translate_with_libre(text, target_lang):
    for server in LIBRE_SERVERS:
        try:
            logger.debug(f"Trying LibreTranslate server: {server}")
            response = requests.post(
                f"{server}/translate", 
                json={"q": text, "source": "en", "target": target_lang, "format": "text"},
                timeout=10
            )
            if response.ok:
                return response.json()["translatedText"]
        except Exception as e:
            logger.debug(f"LibreTranslate server {server} failed: {e}")
            continue
    logger.warning("All LibreTranslate servers failed or returned original text")
    return text

def translate_with_deepl(text, target_lang_code):
    if not DEEPL_API_KEY:
        logger.debug("No DeepL API key found, skipping DeepL translation")
        return text
    
    try:
        logger.debug("Attempting DeepL translation")
        headers = {"Authorization": f"DeepL-Auth-Key {DEEPL_API_KEY}"}
        data = {"text": text, "target_lang": target_lang_code}
        r = requests.post("https://api-free.deepl.com/v2/translate", headers=headers, data=data)
        
        if r.status_code != 200:
            logger.warning(f"DeepL API returned status code {r.status_code}: {r.text}")
            return text
            
        return r.json()["translations"][0]["text"]
    except Exception as e:
        logger.warning(f"DeepL translation failed: {e}")
        return text

def refine_with_gpt(text, translation, target_lang):
    if not has_openai:
        logger.debug("OpenAI integration not available, skipping refinement")
        return translation
    
    try:
        logger.debug("Refining translation with GPT")
        prompt = f"Improve this {LANG_MAP[target_lang]['name']} translation if needed.\nEnglish: {text}\nTranslation: {translation}"
        response = openai.chat.completions.create(
            model="gpt-4-turbo",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning(f"GPT refinement failed: {e}")
        return translation

def apply_translation(text, memory, target_lang):
    original = text.strip()
    if not original:  # Skip empty strings
        return original
        
    # Check memory first
    if original in memory:
        logger.debug(f"Found in memory: '{original[:30]}...' → '{memory[original][:30]}...'")
        return memory[original]

    logger.info(f"Translating: '{original[:50]}...'")
    
    # Try LibreTranslate first
    libre = translate_with_libre(original, target_lang)
    
    # If LibreTranslate failed, try DeepL
    if libre == original:
        logger.debug("LibreTranslate returned original text, trying DeepL")
        deepl = translate_with_deepl(original, LANG_MAP[target_lang]["deepl"])
    else:
        deepl = libre
    
    # If we still have the original text, no translation happened
    if deepl == original:
        logger.warning(f"Translation failed for: '{original[:50]}...'")
        memory[original] = original  # Save to prevent retries
        return original
    
    # Refine with GPT if available
    final = refine_with_gpt(original, deepl, target_lang)
    
    # Save to memory
    memory[original] = final
    logger.debug(f"Translation result: '{final[:50]}...'")
    return final

def inject_code(soup):
    if soup.head:
        for tag in INJECT_HEAD:
            soup.head.append(BeautifulSoup(tag, 'html.parser'))
            logger.debug(f"Injected into head: {tag}")
    if soup.body:
        for tag in INJECT_BODY:
            soup.body.append(BeautifulSoup(tag, 'html.parser'))
            logger.debug(f"Injected into body: {tag}")

def translate_html_file(file_path, target_lang, force=False):
    memory = load_memory()
    output_path = file_path.replace(".html", f"-{target_lang}.html")
    
    if os.path.exists(output_path) and not force:
        logger.info(f"{output_path} exists. Skipping. Use --force to overwrite.")
        return False
    
    try:
        logger.info(f"Processing {file_path}")
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        if not content.strip():
            logger.warning(f"File {file_path} is empty, skipping")
            return False
            
        soup = BeautifulSoup(content, 'html.parser')
    except Exception as e:
        logger.error(f"Failed to read or parse {file_path}: {e}")
        return False

    # Set language attribute
    if soup.html:
        soup.html['lang'] = target_lang
    else:
        logger.warning(f"No <html> tag found in {file_path}")

    # Process links and images
    for tag in soup.find_all(['a', 'img']):
        if tag.name == 'a' and tag.get('href'):
            # Only translate links to .html files
            if tag['href'].endswith('.html') and not tag['href'].endswith(f'-{target_lang}.html'):
                tag['href'] = re.sub(r'\.html$', f'-{target_lang}.html', tag['href'])
                logger.debug(f"Updated link: {tag['href']}")
                
        if tag.name == 'img':
            for attr in ['alt', 'title']:
                if attr in tag.attrs and tag[attr]:
                    tag[attr] = apply_translation(tag[attr], memory, target_lang)

    # Translate text content
    translation_count = 0
    for element in soup.find_all(string=True):
        if element.parent.name in ['script', 'style']:
            continue
            
        clean = element.strip()
        if clean:
            translated = apply_translation(clean, memory, target_lang)
            if translated != clean:
                element.replace_with(translated)
                translation_count += 1

    # Inject custom code
    inject_code(soup)
    
    # Save translated file
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(str(soup.prettify()))
        logger.info(f"Saved: {output_path} with {translation_count} translations")
        save_memory(memory)
        return True
    except Exception as e:
        logger.error(f"Failed to save {output_path}: {e}")
        return False

def find_html_files(directory='.', exclude_patterns=None):
    """Find all HTML files in the given directory, excluding specific patterns"""
    if exclude_patterns is None:
        # Default exclusion patterns - add your injection file names here
        exclude_patterns = [r'head\.html$', r'body\.html$', r'injection\.html$', r'template\.html$', r'-(?:fr|es|de)\.html$']

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    parser.add_argument('--exclude', type=str, help="Comma-separated list of file patterns to exclude (e.g. 'head.html,body.html')")
    parser.add_argument('--include', type=str, help="Comma-separated list of specific files to translate (e.g. 'index.html,about.html')")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Custom exclusion patterns
    exclude_patterns = [r'-(?:fr|es|de)\.html
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
,  # Exclude head.html
            r'body\.html

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Find HTML files
    html_files = find_html_files(args.dir)
    if not html_files:
        logger.error(f"No HTML files found in {args.dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(html_files)} HTML files to process")
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
,  # Exclude body.html
            r'injection\.html

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Find HTML files
    html_files = find_html_files(args.dir)
    if not html_files:
        logger.error(f"No HTML files found in {args.dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(html_files)} HTML files to process")
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
,  # Exclude injection.html
            r'template\.html

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Find HTML files
    html_files = find_html_files(args.dir)
    if not html_files:
        logger.error(f"No HTML files found in {args.dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(html_files)} HTML files to process")
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
,  # Exclude template.html
            r'-(?:fr|es|de)\.html

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Find HTML files
    html_files = find_html_files(args.dir)
    if not html_files:
        logger.error(f"No HTML files found in {args.dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(html_files)} HTML files to process")
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
  # Exclude already translated files
        ]
    
    html_files = []
    
    for file in os.listdir(directory):
        if file.endswith('.html'):
            # Check if file should be excluded
            should_exclude = False
            for pattern in exclude_patterns:
                if re.search(pattern, file):
                    should_exclude = True
                    break
            
            if not should_exclude:
                full_path = os.path.join(directory, file)
                if os.path.isfile(full_path):
                    html_files.append(full_path)
                
    return html_files

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_args()

    # Set debug level if requested
    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate language
    if args.lang not in LANG_MAP:
        logger.error(f"Unsupported language '{args.lang}'. Use one of: {', '.join(LANG_MAP.keys())}")
        sys.exit(1)

    # Check for API keys
    if not DEEPL_API_KEY and not has_openai:
        logger.warning("No translation API keys (DeepL, OpenAI) found. Will try LibreTranslate servers only.")
    
    # Find HTML files
    html_files = find_html_files(args.dir)
    if not html_files:
        logger.error(f"No HTML files found in {args.dir}")
        sys.exit(1)
    
    logger.info(f"Found {len(html_files)} HTML files to process")
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
]  # Always exclude already translated files
    
    if args.exclude:
        # Add user-provided exclusion patterns
        for pattern in args.exclude.split(','):
            pattern = pattern.strip()
            if pattern:
                # Convert filename to regex pattern if it's not already
                if not pattern.startswith('^') and not pattern.endswith('
    
    # Process each file
    success_count = 0
    for file in html_files:
        if translate_html_file(file, args.lang, args.force):
            success_count += 1
            
    logger.info(f"Translation complete: {success_count}/{len(html_files)} files translated to {args.lang}")
,  # Exclude head.html
            r'body\.html

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Translate HTML files.")
    parser.add_argument('--lang', required=True, help="Target language code (e.g., fr, es, de)")
    parser.add_argument('--force', action='store_true', help="Force overwrite of existing translations")
    parser.add_argument('--debug', action='store_true', help="Enable debug logging")
    parser.add_argument('--dir', default=".", help="Directory to search for HTML files")
    args = parser.parse_
