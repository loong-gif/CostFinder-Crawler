# PowerShell script to upload code to GitHub
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Uploading to GitHub" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if Git is installed
try {
    $gitVersion = git --version
    Write-Host "Git found: $gitVersion" -ForegroundColor Green
} catch {
    Write-Host "ERROR: Git is not installed or not in PATH" -ForegroundColor Red
    Write-Host "Please install Git from https://git-scm.com/" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "Step 1: Initializing Git repository..." -ForegroundColor Yellow
git init
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to initialize Git repository" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "Step 2: Adding remote repository..." -ForegroundColor Yellow
try {
    git remote add origin https://github.com/loong-gif/CostFinder-Crawler.git
} catch {
    Write-Host "Warning: Remote may already exist, updating..." -ForegroundColor Yellow
    git remote set-url origin https://github.com/loong-gif/CostFinder-Crawler.git
}

Write-Host ""
Write-Host "Step 3: Adding all files..." -ForegroundColor Yellow
git add .
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to add files" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "Step 4: Committing changes..." -ForegroundColor Yellow
git commit -m "Initial commit: Social Media Finder and Cost Finder Crawler"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to commit" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "Step 5: Setting main branch..." -ForegroundColor Yellow
git branch -M main

Write-Host ""
Write-Host "Step 6: Pushing to GitHub..." -ForegroundColor Yellow
Write-Host "NOTE: You may need to authenticate with your GitHub credentials" -ForegroundColor Cyan
git push -u origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "ERROR: Failed to push to GitHub" -ForegroundColor Red
    Write-Host "This might be due to authentication issues." -ForegroundColor Yellow
    Write-Host "Please check:" -ForegroundColor Yellow
    Write-Host "1. You have write access to the repository" -ForegroundColor Yellow
    Write-Host "2. Your GitHub credentials are correct" -ForegroundColor Yellow
    Write-Host "3. You're using a Personal Access Token if required" -ForegroundColor Yellow
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "SUCCESS! Code has been uploaded to GitHub" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Read-Host "Press Enter to exit"
