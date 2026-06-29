import os
import tempfile
import threading
import time
from ag_core.utils.message_bus import MessageBus, Artifact

def test_concurrent_message_bus_operations():
    db_file = "stress_test_temp.db"
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass
            
    bus = MessageBus(db_path=db_file)
    errors = []
    
    def writer_thread(thread_idx):
        for i in range(1000):
            try:
                art = Artifact(
                    name=f"writer_{thread_idx}_art_{i}",
                    content=f"content_{thread_idx}_{i}",
                    created_by=f"thread_{thread_idx}"
                )
                bus.publish(art)
            except Exception as e:
                errors.append(f"Writer thread {thread_idx} failed: {type(e).__name__}: {str(e)}")
                
    def reader_thread(thread_idx):
        for i in range(1000):
            try:
                bus.retrieve(f"writer_0_art_{i}")
            except Exception as e:
                errors.append(f"Reader thread {thread_idx} failed: {type(e).__name__}: {str(e)}")

    # Start multiple writer and reader threads
    threads = []
    for i in range(10):
        threads.append(threading.Thread(target=writer_thread, args=(i,)))
        threads.append(threading.Thread(target=reader_thread, args=(i,)))
        
    for t in threads:
        t.start()
        
    for t in threads:
        t.join()
        
    print(f"\nTotal concurrency errors: {len(errors)}")
    if errors:
        print("\n--- Concurrency Errors Found ---")
        unique_errors = list(set(errors))
        for err in unique_errors[:20]:
            print(err)
            
    # Try cleaning up database
    time.sleep(0.5)
    for ext in ["", "-wal", "-shm"]:
        f = db_file + ext
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception as e:
                print(f"Could not remove {f}: {e}")
                
    assert len(errors) == 0, f"Errors occurred: {len(errors)}"

if __name__ == "__main__":
    test_concurrent_message_bus_operations()
