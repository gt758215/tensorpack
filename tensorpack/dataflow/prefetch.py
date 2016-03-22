# -*- coding: UTF-8 -*-
# File: prefetch.py
# Author: Yuxin Wu <ppwwyyxx@gmail.com>

import multiprocessing

from .base import DataFlow
from ..utils.concurrency import ensure_procs_terminate

__all__ = ['PrefetchData']

class Sentinel:
    pass

class PrefetchProcess(multiprocessing.Process):
    def __init__(self, ds, queue):
        """
        ds: ds to take data from
        queue: output queue to put results in
        """
        super(PrefetchProcess, self).__init__()
        self.ds = ds
        self.queue = queue

    def run(self):
        self.ds.reset_state()
        try:
            for dp in self.ds.get_data():
                self.queue.put(dp)
        finally:
            self.queue.put(Sentinel())


class PrefetchData(DataFlow):
    def __init__(self, ds, nr_prefetch, nr_proc=1):
        """
        use multiprocess
        """
        self.ds = ds
        self._size = self.ds.size()
        self.nr_proc = nr_proc
        self.nr_prefetch = nr_prefetch

    def size(self):
        return self._size

    def get_data(self):
        queue = multiprocessing.Queue(self.nr_prefetch)
        procs = [PrefetchProcess(self.ds, queue) for _ in range(self.nr_proc)]
        ensure_procs_terminate(procs)
        [x.start() for x in procs]

        end_cnt = 0
        tot_cnt = 0
        try:
            while True:
                dp = queue.get()
                if isinstance(dp, Sentinel):
                    end_cnt += 1
                    if end_cnt == self.nr_proc:
                        break
                    continue
                tot_cnt += 1
                yield dp
                if tot_cnt == self._size:
                    break
        finally:
            queue.close()
            [x.terminate() for x in procs]
