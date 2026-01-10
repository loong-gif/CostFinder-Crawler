@echo off
echo ========================================
echo Uploading to GitHub
echo ========================================
echo.

REM Check if Git is installed
where git >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Git is not installed or not in PATH
    echo Please install Git from https://git-scm.com/
    pause
    exit /b 1
)

echo Step 1: Initializing Git repository...
git init
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to initialize Git repository
    pause
    exit /b 1
)

echo.
echo Step 2: Adding remote repository...
git remote add origin https://github.com/loong-gif/CostFinder-Crawler.git
if %ERRORLEVEL% NEQ 0 (
    echo Warning: Remote may already exist, continuing...
    git remote set-url origin https://github.com/loong-gif/CostFinder-Crawler.git
)

echo.
echo Step 3: Adding all files...
git add .
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to add files
    pause
    exit /b 1
)

echo.
echo Step 4: Committing changes...
git commit -m "Initial commit: Social Media Finder and Cost Finder Crawler"
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to commit
    pause
    exit /b 1
)

echo.
echo Step 5: Setting main branch...
git branch -M main

echo.
echo Step 6: Pushing to GitHub...
echo NOTE: You may need to authenticate with your GitHub credentials
git push -u origin main
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to push to GitHub
    echo This might be due to authentication issues.
    echo Please check:
    echo 1. You have write access to the repository
    echo 2. Your GitHub credentials are correct
    echo 3. You're using a Personal Access Token if required
    pause
    exit /b 1
)

echo.
echo ========================================
echo SUCCESS! Code has been uploaded to GitHub
echo ========================================
pause
