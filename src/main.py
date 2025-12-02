import os
import logging
import json
import sqlite3
import asyncio
from google.genai import types
from typing import List, Dict, Any, Literal
from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool, AgentTool  # AgentTool only needs 'agent'
from google.adk.runners import InMemoryRunner
from google.adk.plugins.logging_plugin import LoggingPlugin
 
 
# ============================= CONFIG & DB SETUP =============================
STORAGE_DB_PATH = "smart_shelf_storage.db"
SESSIONS_DB_PATH = "smart_shelf_sessions.db"
CONFIG_PATH = "smart_shelf_config.json"
SESSION_ID = "concierge_desk"
 
logging.basicConfig(format='%(asctime)s - %(message)s', level=logging.INFO)
 
 
 
def load_config():
    """
    If the config file is not provided, program gives an error and exits.
    This function expects the config file to be in JSON format.
    """
    logging.info("Entering load_config")
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(f"Create {CONFIG_PATH} with your smart shelf layout!")
    logging.info("Exiting load_config")
    with open(CONFIG_PATH) as f:
        return json.load(f)
 
 
 
def init_sessions_db():
    """Create the tiny table that holds the current conversation state."""
    logging.info("Entering init_sessions_db")
    conn = sqlite3.connect(SESSIONS_DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,   
            data TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()
    logging.info("Exiting init_sessions_db")
 
#Init sessions db
init_sessions_db()
 
 
 
def init_storage_db():
    """
    Runs once when the program starts to make sure the shelf layout from the smart_shelf_config.json file is properly initialized in the database.
    """
    logging.info("Entering init_storage_db")
    config = load_config()
    conn = sqlite3.connect(STORAGE_DB_PATH)
    c = conn.cursor()
 
    c.execute("""CREATE TABLE IF NOT EXISTS storage_space_details (
                 spot_id TEXT PRIMARY KEY, size TEXT, location TEXT,
                 apartment TEXT DEFAULT '', occupied INTEGER DEFAULT 0)""")
    
    #This will elimate inserting duplicate records when the script is rerun
    existing = {row[0] for row in c.execute("SELECT spot_id FROM storage_space_details").fetchall()}
    to_add = [
        (item["spot_id"], item["size"], item.get("location", "Main Area"))
        for item in config["storage_space_details"]
        if item["spot_id"] not in existing
    ]
    if to_add:
        c.executemany("INSERT INTO storage_space_details (spot_id, size, location) VALUES (?,?,?)", to_add)
        logging.info(f"Added {len(to_add)} storage_space_details from smart_shelf_config.json")
    conn.commit()
    conn.close()
    logging.info("Exiting init_storage_db")
 
 
 
#Making the storage DataBase ready before everything
init_storage_db() 
 
 
 
# ============================= STATE AND CUSTOM TOOL FUNCTIONS =============================
# LLM's are not allowed to use SQl queries to avoid race conditions, complete traceability, and hallucinations. 
# All queries are executes by these tiny, atomic and testable functions.
 
 
 
def get_or_create_session() -> Dict[str, Any]:
    """Return current session dict. Creates empty row automatically if first time."""
    logging.info("Entering get_or_create_session")
    conn = sqlite3.connect(SESSIONS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT data FROM sessions WHERE session_id = ?", (SESSION_ID,))
    row = cur.fetchone()
 
    if row is None:
        # create empty session for the first time
        conn.execute("INSERT INTO sessions (session_id, data) VALUES (?, ?)",
                     (SESSION_ID, "{}"))
        conn.commit()
        conn.close()
        return {}
 
    conn.close()
    logging.info("Exiting get_or_create_session")
    return json.loads(row["data"])
 
 
 
def set_in_session(key: str, value: Any):
    """Update the conversation state, and create a new session if one doesn’t already exist."""
    logging.info("Entering set_in_session")
    session = get_or_create_session()
    session[key] = value
    conn = sqlite3.connect(SESSIONS_DB_PATH)
    conn.execute('''
        INSERT INTO sessions (session_id, data)
        VALUES (?, ?)
        ON CONFLICT(session_id) DO UPDATE SET data = excluded.data
    ''', (SESSION_ID, json.dumps(session, ensure_ascii=False)))
    conn.commit()
    conn.close()
    logging.info("Exiting set_in_session")
 
 
 
def delete_session() -> str:
    """Clear the conversation state, usually called at the end of a successful flow."""
    logging.info("Entering delete_session")
    conn = sqlite3.connect(SESSIONS_DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM sessions WHERE session_id = ?", (SESSION_ID,))
    
    deleted_count = cur.rowcount  # how many rows were actually deleted
    
    conn.commit()
    conn.close()
    
    logging.info("Exiting delete_session")
    if deleted_count > 0:
        return "Session deleted successfully."
    else:
        return "No session found to delete."
 
 
 
def set_size(size: Literal["S", "M", "L"]) -> dict[str, Any]:
    """Keeps track of the current package size we're working with."""
    logging.info("Entering set_size")
    set_in_session("size", size)
    updated_state = get_or_create_session()
    logging.info("Exiting set_size")
    return updated_state
 
 
 
def set_apartment(apartment: str) -> dict[str, Any]:
    """Save the apartment number (always in uppercase, so '3b' and '3B' are treated the same)."""
    logging.info("Entering set_apartment")
    set_in_session("apartment_number", apartment.upper())
    updated_state = get_or_create_session()
    logging.info("Exiting set_apartment")
    return updated_state
 
 
 
def find_available_spots() -> dict[str, Any]:
    """
    Query the database for available storage spots based on the specified package size (S, M, or L).
    Uses an SQL `SELECT` query to fetch spot IDs and locations of storage_space_details that are unoccupied. 
    updates the session with the details of the available spot and then returns the updated session state. If no spots are available, it marks that in the session instead.
    """
    logging.info("Entering find_available_spots")
    state = get_or_create_session()
    conn = sqlite3.connect(STORAGE_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT spot_id, location FROM storage_space_details WHERE size=? AND occupied=0", (state["size"],))
    rows = c.fetchall()
    conn.close()
    spots = [{"spot_id": r[0], "location": r[1]} for r in rows]
    print(f"[TOOL] Available {state["size"]} spots: {len(spots)}")
    if spots:
        set_in_session("spot_available", True)
        set_in_session("spot_id", spots[0]["spot_id"])
        set_in_session("spot_location", spots[0]["location"])
    elif not spots:
        set_in_session("spot_available", False)
    updated_state = get_or_create_session()
    logging.info("Exiting find_available_spots")
    return updated_state
 
 
 
def reserve_spot() -> dict[str, Any]:
    """
    Reserve a specific storage spot for an apartment by updating the database and associate it with the apartment number from the session.
    Uses an SQL `UPDATE` query to mark the spot as occupied and associate it with the given apartment.
    If the spot is available, it updates the session to indicate a successful reservation. Else, it sets the status to "failed".
    If the spot is already taken or doesn't exist, it returns the updated session state.
    """
    logging.info("Entering reserve_spot")
    state = get_or_create_session()
    conn = sqlite3.connect(STORAGE_DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE storage_space_details SET occupied=1, apartment=? WHERE spot_id=? AND occupied=0",
              (state["apartment_number"], state["spot_id"]))
    success = c.rowcount > 0
    conn.commit()
    conn.close()
    print(f"Row count is: {c.rowcount}")
    result = "Success" if success else "Failed: Spot already taken or doesn't exist"
    print(f"[TOOL] reserve_spot({state["spot_id"]}, {state["apartment_number"]}) → {result}")
    if success:
        set_in_session("reservation_status", True)
    else:
        set_in_session("reservation_status", False)
    updated_state = get_or_create_session()
    logging.info("Exiting reserve_spot")
    return updated_state
 
 
 
# List[Dict[str, str]
def find_packages() -> dict[str, Any]:
    """
    Fetch all packages stored for a given apartment by querying the database. 
    Uses SQL to retrieve the spot ID, size, and location of the packages stored for that apartment.
    It returns a list of spot details and updates the session with the package info, or marks 'no packages found' if none exist
    """
    logging.info("Entering find_packages")
    state = get_or_create_session()
    conn = sqlite3.connect(STORAGE_DB_PATH)
    c = conn.cursor()
    c.execute("SELECT spot_id, size, location FROM storage_space_details WHERE apartment=? AND occupied=1", (state["apartment_number"],))
    rows = c.fetchall()
    conn.close()
    pkgs = [{"spot_id": r[0], "size": r[1], "location": r[2]} for r in rows]
    print(f"[TOOL] Packages for {state["apartment_number"]}: {len(pkgs)}")
    if c.rowcount > 0:
        set_in_session("packages_found", True)
    else:
        set_in_session("packages_found", False)
    set_in_session("packages_info", pkgs)
    updated_state = get_or_create_session()
    logging.info("Exiting find_packages")
    return updated_state
 
 
 
def release_packages() -> dict[str, Any]:
    """
    Release all packages stored for a given apartment by updating the database. 
    Uses an SQL `UPDATE` query to set the storage spot as unoccupied and remove the apartment association.
    Returns a summary of how many packages were released for an apartment, or a message indicating no packages were found if there were none to release, and updates the session with the release status.
    """
    logging.info("Entering release_packages")
    state = get_or_create_session()
    conn = sqlite3.connect(STORAGE_DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE storage_space_details SET occupied=0, apartment='' WHERE apartment=?", (state["apartment_number"],))
    count = c.rowcount
    conn.commit()
    conn.close()
    result = f"Released {count} package(s)" if count > 0 else "No packages found to release"
    print(f"[TOOL] release_packages({state["apartment_number"]}) → {result}")
    if count > 0:
        set_in_session("release_status", True)
    else:
        set_in_session("release_status", False)
    updated_state = get_or_create_session()
    logging.info("Exiting release_packages")
    return updated_state
 
 
 
# Make the functions as ADK tools to keep the LLM fully isolated from the database.
delete_session_tool = FunctionTool(delete_session)
set_size_tool = FunctionTool(set_size)
set_apartment_tool = FunctionTool(set_apartment)
find_spots_tool = FunctionTool(find_available_spots)
reserve_tool = FunctionTool(reserve_spot)
find_pkgs_tool = FunctionTool(find_packages)
release_tool = FunctionTool(release_packages)
 
 
 
# ============================= AGENT DESIGN & BEHAVIOUR =============================
#The SmartShelfInterfaceAgent is a simple router which is fast, cost-effective, and reliable.
#The SmartShelfStorageAgent and SmartShelfRetrievalAgent use small, highly focused prompts, resulting in almost no hallucination.
# The Agents are isolated from the database and uses custom tools to interact with the system.
 
 
 
# SmartShelfStorageAgent handles the entire process of reserving storage_space_details for package storage. 
smart_shelf_storage_agent = LlmAgent(
    name="SmartShelfStorageAgent",
    description="Handles storing packages",
    model="gemini-2.5-pro",        
    instruction="""
You are a Package Storage Specialist.
 
You will receive user requests.
 
If the request contains package size and apartment number,
    1. Call delete_session_tool and **wait for it to finish and return response**.
    2. Call set_size_tool with the size literal (S/M/L) corresponding to the package size and **wait for it to finish and return response**.
    3. Call set_apartment_tool with the apartment number and **wait for it to finish and return response**.
    4. Call find_spots_tool and **wait for it to finish and return response**.
    5. Using the information from the return value of find_spots_tool,
        5a. If spot is found provide details and ask user to confirm with yes or decline with no and **wait for user input**.
        5b. Else if spot is not found, call delete_session_tool and **wait for it to finish and return response** and then **update the user with complete information**.
 
If the request contains yes,
    a. Call reserve_tool and **wait for it to finish and return response**.
    b. Preserve the return value of reserve_tool.
    c. Call delete_session_tool and **wait for it to finish and return response**.
    d. Using the information from the return value of reserve_tool **update the user with complete information**.
 
If the request contains no, call delete_session_tool and **wait for it to finish and return response** and then **update the user about the storage cancellation**.
 
If the request contains anything else, call delete_session_tool and **wait for it to finish and return response** and then seek further clarification from the user.
""",
    tools=[set_size_tool, set_apartment_tool, find_spots_tool, reserve_tool, delete_session_tool]
)
 
 
 
# SmartShelfRetrievalAgenthandles handles the process of retrieving stored packages for a specific apartment.
smart_shelf_retrieval_agent = LlmAgent(
    name="SmartShelfRetrievalAgent",
    description="Handles retrieving packages",
    model="gemini-2.5-pro",
    instruction="""
You are a Package Retrieval Specialist.
 
You will receive a request user requests.
 
If the request contains apartment number,
    1. Call delete_session_tool and **wait for it to finish and return response**.
    2. Call set_apartment_tool with apartment number and **wait for it to finish and return response**.
    3. Call find_pkgs_tool and **wait for it to finish and return response**.
    4. Using the information from the return value of find_pkgs_tool,
        4a. If packages are found provide details and ask user to confirm with yes or decline with no and **wait for user input**.
        4b. Else if packages are not found, call delete_session_tool and **wait for it to finish and return response** and then **update the user with complete information**.
 
If the request contains yes,
    a. Call release_tool and **wait for it to finish and return response**.
    b. Preserve the return value of release_tool.
    c. Call delete_session_tool and **wait for it to finish and return response**.
    d. Using the information from the return value of release_tool **update the user with complete information**.
 
If the request contains no, call delete_session_tool and **wait for it to finish and return response** and then **update the user about the release cancellation**.
 
If the request contains anything else, call delete_session_tool and **wait for it to finish and return response** and then seek further clarification from the user.
""",
    tools=[set_apartment_tool, find_pkgs_tool, release_tool, delete_session_tool]
)
 
 
 
# SmartShelfInterfaceAgent acts as the central routing agent for package-related requests.
# This agent listens for user input and routes the request to either the SmartShelfStorageAgent (for storing packages) or the SmartShelfRetrievalAgent (for retrieving packages).
smart_shelf_interface_agent = LlmAgent(
    name="SmartShelfInterfaceAgent",
    model="gemini-2.5-pro",
    instruction="""
You are the friendly Package Storage Assistant.
 
- If user wants to store/drop/leave a package → call SmartShelfStorageAgent
- If user wants to pick up/retrieve/collect → call SmartShelfRetrievalAgent
- Otherwise ask for clarification.
 
You never speak directly — you only route.
""",
    tools=[
        AgentTool(agent=smart_shelf_storage_agent),
        AgentTool(agent=smart_shelf_retrieval_agent)
    ]
)
 
 
 
# ============================= RUNNER & SESSION =============================
#Using InMemoryRunner with a single persistent session is ideal for managing a building or concierge team. 
#The LoggingPlugin automatically provides an audit trail, tracking who stored what and when.
 
async def main():
    logging.info("Entering main")
    print("Package Storage Assistant\n")
 
    runner = InMemoryRunner(
        agent=smart_shelf_interface_agent,
        app_name="SmartShelf",
        plugins=[LoggingPlugin()]
    )
 
    user_id = "concierge_team"
    session_id = SESSION_ID
 
    await runner.session_service.create_session(
        app_name="SmartShelf", user_id=user_id, session_id=session_id
    )
 
    print("Ready! Type your request (or 'quit')\n")
 
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"quit", "q", "exit", ""}:
            print("Goodbye!")
            logging.info("Exiting main")
            break
 
        message = types.Content(
            role="user",
            parts=[types.Part(text=user_input)]
        )
 
        response_text = ""
 
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message
        ):
            # Skip events that have no text (tool calls, etc.)
            if (hasattr(event, "content") and 
                event.content and 
                event.content.parts and 
                hasattr(event.content.parts[0], "text") and 
                event.content.parts[0].text):
                
                response_text += event.content.parts[0].text
 
        # Final cleanup
        response_text = response_text.strip()
        if not response_text:
            response_text = "I'm thinking..."  # fallback
 
        print(f"\nAssistant: {response_text}\n")
 
if __name__ == "__main__":
    asyncio.run(main())

