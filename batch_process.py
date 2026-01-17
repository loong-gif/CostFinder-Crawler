"""
Batch processing script
Reads URL list from input file and batch crawls social media information
"""

import csv
import json
import os
import sys
import io
import asyncio
from datetime import datetime
from typing import List, Dict, Any
from crawler.async_social_media_finder import AsyncSocialMediaFinder
from utils.logger import Logger
from utils.url_validator import URLValidator

# Set stdout encoding to UTF-8 (Windows compatible)
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


class BatchProcessor:
    """Batch processor"""

    def __init__(self, input_file: str = "input_website_list_cleaned.txt", max_workers: int = 1) -> None:
        """
        Initialize batch processor.
        
        Args:
            input_file: Input file path
            max_workers: Maximum number of concurrent workers (deprecated, kept for compatibility)
                         Note: Now uses async sequential processing, one at a time
        """
        self.input_file = input_file
        self.max_workers = max_workers  # Deprecated, kept for compatibility
        self.logger = Logger.get_logger(self.__class__.__name__)
        self.results = []
        self.stats = {
            "total_urls": 0,
            "processed": 0,
            "success": 0,
            "failed": 0,
        }
        
        # Initialize platform-specific stats dynamically
        import config
        for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
            self.stats[f"total_{platform}"] = 0

    def read_urls(self) -> List[str]:
        """
        Read URL list from input file and filter to keep only unique domains.
        
        Returns:
            List[str]: URL list with unique domains only
        """
        urls = []
        seen_domains = set()
        unique_urls = []
        
        try:
            with open(self.input_file, "r", encoding="utf-8") as f:
                for line in f:
                    url = line.strip()
                    
                    # Filter empty lines
                    if not url:
                        continue
                    
                    # Normalize URL (preserves original protocol if present)
                    normalized_url = URLValidator.normalize_url(url)
                    
                    # Extract and normalize domain
                    domain = URLValidator.get_normalized_domain(normalized_url)
                    
                    # Skip if domain is empty or invalid
                    if not domain:
                        self.logger.warning(f"Invalid or empty domain for URL: {url}")
                        continue
                    
                    # Only add URL if we haven't seen this domain before
                    # This ensures http://example.com and https://example.com are treated as the same domain
                    if domain not in seen_domains:
                        seen_domains.add(domain)
                        unique_urls.append(normalized_url)
                    else:
                        self.logger.debug(f"Skipping duplicate domain: {domain} (URL: {normalized_url})")
            
            self.logger.info(
                f"Read {len(urls)} URLs from {self.input_file}, "
                f"filtered to {len(unique_urls)} unique domains"
            )
            return unique_urls
            
        except FileNotFoundError:
            self.logger.error(f"File not found: {self.input_file}")
            return []
        except Exception as e:
            self.logger.error(f"Failed to read file: {str(e)}")
            return []

    def process_all(self) -> None:
        """Process all URLs."""
        # Read URL list
        urls = self.read_urls()
        
        if not urls:
            self.logger.error("No valid URLs found")
            print("❌ No valid URLs found")
            return
        
        self.stats["total_urls"] = len(urls)
        self.logger.info(f"Starting batch processing of {len(urls)} websites")
        
        print("=" * 70)
        print(f"🚀 Starting batch processing of {len(urls)} websites")
        print("=" * 70)
        
        # Process URLs with async sequential execution
        asyncio.run(self._process_async_sequential(urls))
        
        # Save results
        self.save_results()
        self.print_summary()

    def save_results(self) -> None:
        """Save processing results to files."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save complete results (JSON format)
        json_filename = f"results_{timestamp}.json"
        try:
            with open(json_filename, "w", encoding="utf-8") as f:
                json.dump({
                    "stats": self.stats,
                    "results": self.results,
                    "processed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }, f, ensure_ascii=False, indent=2)
            print(f"\n✅ Complete results saved to: {json_filename}")
        except Exception as e:
            self.logger.error(f"Failed to save JSON file: {str(e)}")
        
        # Save summary results (CSV format) - output fields as specified
        csv_filename = f"results_summary_{timestamp}.csv"
        try:
            with open(csv_filename, "w", encoding="utf-8-sig", newline='') as f:
                writer = csv.writer(f)
                # Header row
                writer.writerow(["Domain", "Instagram Account Name", "Instagram Home Page", "Facebook Account Name", "Facebook Homepage"])
                for result in self.results:
                    # Only take first Instagram/Facebook account name and homepage; empty if none
                    url = result.get("url", "")
                    ig_acct = result["instagram"][0]["username"] if result.get("instagram") and len(result["instagram"]) > 0 else ""
                    ig_url = result["instagram"][0]["profile_url"] if result.get("instagram") and len(result["instagram"]) > 0 else ""
                    fb_acct = result["facebook"][0]["username"] if result.get("facebook") and len(result["facebook"]) > 0 else ""
                    fb_url = result["facebook"][0]["profile_url"] if result.get("facebook") and len(result["facebook"]) > 0 else ""
                    writer.writerow([url, ig_acct, ig_url, fb_acct, fb_url])
            print(f"✅ Summary results saved to: {csv_filename}")
        except Exception as e:
            self.logger.error(f"Failed to save CSV file: {str(e)}")
        
        # Save results containing only found social media (simplified version)
        found_filename = f"social_media_found_{timestamp}.txt"
        try:
            with open(found_filename, "w", encoding="utf-8") as f:
                f.write("=" * 70 + "\n")
                f.write("Websites with Social Media Accounts Found\n")
                f.write("=" * 70 + "\n\n")
                
                found_count = 0
                import config
                for result in self.results:
                    # Check if any platform has results
                    has_social_media = any(
                        len(result.get(platform, [])) > 0 
                        for platform in config.SOCIAL_MEDIA_PLATFORMS.keys()
                    )
                    
                    if has_social_media:
                        found_count += 1
                        f.write(f"\nWebsite: {result['url']}\n")
                        f.write("-" * 70 + "\n")
                        
                        # Write all platforms dynamically
                        platform_icons = {
                            "instagram": "📷",
                            "facebook": "👥",
                            "twitter": "🐦",
                            "linkedin": "💼",
                            "youtube": "📺",
                            "tiktok": "🎵",
                            "pinterest": "📌",
                            "snapchat": "👻",
                            "whatsapp": "💬",
                        }
                        
                        for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
                            accounts = result.get(platform, [])
                            if accounts:
                                icon = platform_icons.get(platform, "🔗")
                                f.write(f"{icon} {platform.capitalize()}:\n")
                                for acc in accounts:
                                    f.write(f"  - {acc['username']}: {acc['profile_url']}\n")
                        
                        f.write("\n")
                
                f.write("=" * 70 + "\n")
                f.write(f"Total websites with social media found: {found_count}\n")
            
            print(f"✅ Social media information saved to: {found_filename}")
        except Exception as e:
            self.logger.error(f"Failed to save TXT file: {str(e)}")

    async def _process_single_url_async(self, url: str) -> Dict[str, Any]:
        """
        Process a single URL asynchronously and return result.
        
        Args:
            url: URL to process
            
        Returns:
            Dict containing result and URL
        """
        try:
            async with AsyncSocialMediaFinder() as finder:
                result = await finder.find(url)
                return {"url": url, "result": result, "error": None}
        except Exception as e:
            self.logger.error(f"Error processing {url}: {str(e)}", exc_info=True)
            return {"url": url, "result": None, "error": str(e)}
    
    async def _process_async_sequential(self, urls: List[str]) -> None:
        """
        Process URLs sequentially using async/await.
        Waits for each URL to complete before starting the next one.
        
        Args:
            urls: List of URLs to process
        """
        processed_count = 0
        
        try:
            # Process URLs one by one sequentially
            for url in urls:
                processed_count += 1
                
                try:
                    task_result = await self._process_single_url_async(url)
                    result = task_result["result"]
                    error = task_result["error"]
                    
                    if error:
                        self.stats["failed"] += 1
                        self.stats["processed"] += 1
                        print(f"\n[{processed_count}/{len(urls)}] ❌ {url} - Error: {error}")
                    elif result:
                        self.results.append(result)
                        self.stats["processed"] += 1
                        
                        if result["status"] == "success":
                            self.stats["success"] += 1
                            
                            # Count all platforms dynamically
                            import config
                            platform_counts = []
                            for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
                                count = len(result.get(platform, []))
                                if count > 0:
                                    platform_counts.append(f"{platform.capitalize()}: {count}")
                                    # Update platform stats
                                    if f"total_{platform}" in self.stats:
                                        self.stats[f"total_{platform}"] += count
                            
                            if platform_counts:
                                print(f"\n[{processed_count}/{len(urls)}] ✅ {url} - {', '.join(platform_counts)}")
                            else:
                                print(f"\n[{processed_count}/{len(urls)}] ✅ {url} - No social media found")
                        else:
                            self.stats["failed"] += 1
                            print(f"\n[{processed_count}/{len(urls)}] ❌ {url} - {result['message']}")
                    
                    # Show progress
                    progress = (processed_count / len(urls)) * 100
                    print(f"📊 Overall progress: {progress:.1f}% ({processed_count}/{len(urls)})")
                    
                except Exception as e:
                    self.logger.error(f"Error processing {url}: {str(e)}")
                    self.stats["failed"] += 1
                    self.stats["processed"] += 1
                    print(f"\n[{processed_count}/{len(urls)}] ❌ {url} - Error occurred: {str(e)}")
                        
        except KeyboardInterrupt:
            self.logger.warning("User interrupted processing")
            print("\n\n⚠️  User interrupted, saving processed results...")

    def print_summary(self) -> None:
        """Print processing summary."""
        import config
        print("\n" + "=" * 70)
        print("📊 Processing Complete - Statistics Summary")
        print("=" * 70)
        print(f"Total URL count:        {self.stats['total_urls']}")
        print(f"Processed:              {self.stats['processed']}")
        print(f"Success:                {self.stats['success']}")
        print(f"Failed:                 {self.stats['failed']}")
        print(f"Success rate:           {(self.stats['success'] / self.stats['processed'] * 100):.1f}%" if self.stats['processed'] > 0 else "N/A")
        print("-" * 70)
        
        # Print platform-specific stats
        total_accounts = 0
        for platform in config.SOCIAL_MEDIA_PLATFORMS.keys():
            count = self.stats.get(f"total_{platform}", 0)
            if count > 0:
                print(f"{platform.capitalize()} found:        {count} accounts")
                total_accounts += count
        
        print(f"Total:                  {total_accounts} accounts")
        print("=" * 70)


def main():
    """Main function"""
    print("=" * 70)
    print("🔍 Social Media Finder - Batch Processing Mode")
    print("=" * 70)
    
    # Check if input file exists
    input_file = "input_website_list_cleaned.txt"
    if not os.path.exists(input_file):
        print(f"❌ Error: Input file not found '{input_file}'")
        print("Please ensure the file exists and the path is correct")
        return
    
    # Create batch processor
    processor = BatchProcessor(input_file)
    
    # Start processing
    try:
        processor.process_all()
    except KeyboardInterrupt:
        print("\n\n⚠️  Program interrupted by user")
    except Exception as e:
        print(f"\n❌ Error occurred: {str(e)}")


if __name__ == "__main__":
    main()

