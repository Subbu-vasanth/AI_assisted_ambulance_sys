What each teammate does
Clone the repository
git clone git@github.com:Subbu-vasanth/AI_assisted_ambulance_sys.git
Go into the project
cd AI_assisted_ambulance_sys
Create their own branch
For example:
git checkout -b feature/backend
or
git checkout -b feature/frontend
Push the new branch
git push -u origin feature/backend
Daily workflow
Every teammate works like this:
git pull origin main        # Get latest changes
git checkout feature/backend
# Make changes
git add .
git commit -m "Implemented backend APIs"
git push
