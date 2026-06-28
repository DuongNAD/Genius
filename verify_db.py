import sqlite3
import sys
import os

def verify():
    from ag_core.utils.db import get_db_path
    db_path = get_db_path()
    
    print(f"Connecting to database at: {db_path}")
    if not os.path.exists(db_path):
        print(f"Warning: Database file {db_path} does not exist yet. Initializing schema to verify structure...")
        # Make sure we can initialize it to test verification
        from ag_core.utils.db import init_db
        init_db()

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Verify table presence by selecting from sqlite_master
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cursor.fetchall()]
        
        if "conversations" not in tables:
            print("Error: Table 'conversations' does not exist.")
            sys.exit(1)
        if "agent_logs" not in tables:
            print("Error: Table 'agent_logs' does not exist.")
            sys.exit(1)
            
        # Verify conversations structure
        cursor.execute("PRAGMA table_info(conversations)")
        conversations_cols = {row[1]: row[2] for row in cursor.fetchall()}
        expected_conversations = {"id", "timestamp", "prompt", "result"}
        for col in expected_conversations:
            if col not in conversations_cols:
                print(f"Error: Column '{col}' is missing in conversations table.")
                sys.exit(1)
                
        # Verify agent_logs structure
        cursor.execute("PRAGMA table_info(agent_logs)")
        agent_logs_cols = {row[1]: row[2] for row in cursor.fetchall()}
        expected_agent_logs = {"id", "timestamp", "task_id", "agent_name", "prompt", "result", "status", "error"}
        for col in expected_agent_logs:
            if col not in agent_logs_cols:
                print(f"Error: Column '{col}' is missing in agent_logs table.")
                sys.exit(1)
                
        print("Database schema successfully verified.")
        
        # Print records in conversations
        cursor.execute("SELECT * FROM conversations")
        conv_rows = cursor.fetchall()
        print(f"\n--- conversations ({len(conv_rows)} records) ---")
        for row in conv_rows:
            try:
                print(row)
            except UnicodeEncodeError:
                safe_row = str(row).encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8')
                print(safe_row)
            
        # Print records in agent_logs
        cursor.execute("SELECT * FROM agent_logs")
        log_rows = cursor.fetchall()
        print(f"\n--- agent_logs ({len(log_rows)} records) ---")
        for row in log_rows:
            try:
                print(row)
            except UnicodeEncodeError:
                safe_row = str(row).encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8')
                print(safe_row)
            
        conn.close()
        print("\nVerification successful. Exiting with code 0.")
        sys.exit(0)
    except Exception as e:
        print(f"Database verification failed with error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    verify()
