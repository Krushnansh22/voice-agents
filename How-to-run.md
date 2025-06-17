## ⚙️ Project Setup Instructions

### 1. 🗄️ Install MongoDB & MongoDB Compass
- Download and install MongoDB and MongoDB Compass.
- Create a database named: `voice_assistant_db`

---

### 2. 🔐 Create `.env` File
Create a `.env` file in the project root and add the following environment variables:

```env
PLIVO_AUTH_ID=***********************
PLIVO_AUTH_TOKEN=***********************
PLIVO_FROM_NUMBER=***********************
PLIVO_TO_NUMBER=***********************
PLIVO_ANSWER_XML=https://e8b8-2402-8100-3118-723-b489-ea88-ee5d-1820.ngrok-free.app/webhook

AZURE_OPENAI_API_KEY_P=***********************
AZURE_OPENAI_API_ENDPOINT_P=***********************

HOST_URL=wss://e8b8-2402-8100-3118-723-b489-ea88-ee5d-1820.ngrok-free.app
PORT=8090
```
### 3. 📄 Update Excel File
Edit the Hospital_Records.xlsx file to set your target phone numbers.

You can also automate this using the ExcelCreation.py script:

```bash
python ExcelCreation.py
```
### 4. 🌐 Start Ngrok Tunnel
Download and install ngrok, then start a tunnel on port 8090:

```bash
ngrok http 8090
```
### 5. 🚀 Run the Application
Start the main FastAPI app:

```bash
python main.py
```
### 6. 🖥️ Access the Dashboard
Open your browser and navigate to:
```
http://<your-ngrok-subdomain>.ngrok-free.app/dashboard
``` 
