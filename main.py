"""
Main program entry point
Provides command-line interface and example usage
"""

import json
import sys
import io
from crawler.social_media_finder import SocialMediaFinder
from utils.logger import Logger

# Set stdout encoding to UTF-8 (Windows compatible)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


def print_result(result: dict):
    """
    Format and print result.
    
    Args:
        result: Search result dictionary
    """
    print("\n" + "=" * 60)
    print(f"Website: {result['url']}")
    print(f"Status: {result['status']}")
    print(f"Message: {result['message']}")
    print(f"Found at: {result['found_at']}")
    print("-" * 60)
    
    # Instagram results
    if result['instagram']:
        print(f"\n📷 Instagram accounts ({len(result['instagram'])}):")
        for idx, account in enumerate(result['instagram'], 1):
            print(f"  {idx}. Username: {account['username']}")
            print(f"     Link: {account['profile_url']}")
    else:
        print("\n📷 Instagram: Not found")
    
    # Facebook results
    if result['facebook']:
        print(f"\n👥 Facebook accounts ({len(result['facebook'])}):")
        for idx, account in enumerate(result['facebook'], 1):
            print(f"  {idx}. Username: {account['username']}")
            print(f"     Link: {account['profile_url']}")
    else:
        print("\n👥 Facebook: Not found")
    
    print("=" * 60 + "\n")


def save_result_to_json(result: dict, filename: str = "result.json"):
    """
    Save result to JSON file.
    
    Args:
        result: Search result
        filename: Output filename
    """
    try:
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"✅ Result saved to: {filename}")
    except Exception as e:
        print(f"❌ Save failed: {str(e)}")


def main():
    """Main function"""
    logger = Logger.get_logger("Main")
    
    print("=" * 60)
    print("🔍 Social Media Finder - Social Media Information Crawler")
    print("=" * 60)
    
    # Check command line arguments
    if len(sys.argv) > 1:
        target_url = sys.argv[1]
    else:
        # Interactive input
        target_url = input("\nPlease enter target website URL: ").strip()
    
    if not target_url:
        logger.error("No target URL provided")
        print("❌ Error: No target URL provided")
        return
    
    # Create crawler instance
    with SocialMediaFinder() as finder:
        # Execute search
        result = finder.find(target_url)
        
        # Print result
        print_result(result)
        
        # Ask if save result
        save_option = input("Save result to JSON file? (y/n): ").strip().lower()
        if save_option == 'y':
            filename = input("Enter filename (default: result.json): ").strip()
            if not filename:
                filename = "result.json"
            save_result_to_json(result, filename)


def example_usage():
    """Example usage"""
    print("\n" + "=" * 60)
    print("Example Usage:")
    print("=" * 60)
    
    # Example 1: Single website search
    print("\n📌 Example 1: Search single website")
    with SocialMediaFinder() as finder:
        result = finder.find("https://example.com")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    
    # Example 2: Batch search
    print("\n📌 Example 2: Batch search multiple websites")
    urls = [
        "https://example1.com",
        "https://example2.com",
    ]
    
    with SocialMediaFinder() as finder:
        results = finder.find_multiple(urls)
        for result in results:
            print_result(result)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Program interrupted by user")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ Error occurred: {str(e)}")
        sys.exit(1)

