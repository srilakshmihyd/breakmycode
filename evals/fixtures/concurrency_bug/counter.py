import threading


def increment_with_two_workers():
    counter = 0
    barrier = threading.Barrier(2)

    def worker():
        nonlocal counter
        current = counter
        barrier.wait()
        counter = current + 1

    workers = [threading.Thread(target=worker) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    return counter

