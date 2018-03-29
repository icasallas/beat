import multiprocessing
from logging import getLogger
import traceback
from functools import wraps
import signal
from itertools import count
import numpy as num


logger = getLogger('paripool')


def exception_tracer(func):
    """
    Function decorator that returns a traceback if an Error is raised in
    a child process of a pool.
    """
    @wraps(func)
    def wrapped_func(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            msg = "{}\n\nOriginal {}".format(e, traceback.format_exc())
            print('Exception in ' + func.__name__)
            raise type(e)(msg)

    return wrapped_func


class TimeoutException(Exception):
    """
    Exception raised if a per-task timeout fires.
    """
    def __init__(self, jobstack=[]):
        super(TimeoutException, self).__init__()
        self.jobstack = jobstack


# http://stackoverflow.com/questions/8616630/time-out-decorator-on-a-multprocessing-function
def overseer(timeout):
    """
    Function decorator that raises a TimeoutException exception
    after timeout seconds, if the decorated function did not return.
    """

    def decorate(func):
        def timeout_handler(signum, frame):
            raise TimeoutException(traceback.format_stack())

        @wraps(func)
        def wrapped_f(*args, **kwargs):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)

            result = func(*args, **kwargs)

            # Old signal handler is restored
            signal.signal(signal.SIGALRM, old_handler)
            signal.alarm(0)  # Alarm removed
            return result

        wrapped_f.__name__ = func.__name__
        return wrapped_f

    return decorate


class WatchedWorker(object):
    """
    Wrapper class for parallel execution of a task.

    Parameters
    ----------
    task : function to execute
    work : List
        of arguments to specified function
    timeout : int
        time [s] after which worker is fired, default 65536s
    """

    def __init__(self, task, work, timeout=0xFFFF):
        self.function = task
        self.work = work
        self.timeout = timeout

    def run(self):
        """
        Start working on the task!
        """
        try:
            return self.function(*self.work)
        except TimeoutException:
            logger.warn('Worker timed out! Fire him! Returning: None!')
            return None


def _pay_worker(worker):
    """
    Wrapping function for the pool start instance.
    """
    return overseer(worker.timeout)(worker.run)()


def paripool(
        function, workpackage, nprocs=None, chunksize=1, timeout=0xFFFF,
        initializer=None, initargs=()):
    """
    Initialises a pool of workers and executes a function in parallel by
    forking the process. Does forking once during initialisation.

    Parameters
    ----------
    function : function
        python function to be executed in parallel
    workpackage : list
        of iterables that are to be looped over/ executed in parallel usually
        these objects are different for each task.
    nprocs : int
        number of processors to be used in paralell process
    chunksize : int
        number of work packages to throw at workers in each instance
    timeout : int
        time [s] after which processes are killed, default: 65536s
    initialiser : function
        to init pool with may be container for shared arrays
    initargs : tuple
        of arguments for the initialiser
    """

    def start_message(*globals):
        logger.debug('Starting %s' % multiprocessing.current_process().name)

    def callback(result):
        logger.info('\n Feierabend! Done with the work!')

    if nprocs is None:
        nprocs = multiprocessing.cpu_count()

    if chunksize is None:
        chunksize = 1

    if nprocs == 1:
        for work in workpackage:
            yield [function(*work)]

    else:
        pool = multiprocessing.Pool(
            processes=nprocs,
            initializer=initializer,
            initargs=initargs)

        logger.info('Worker timeout after %i second(s)' % timeout)

        workers = [
            WatchedWorker(function, work, timeout) for work in workpackage]

        pool_timeout = int(len(workpackage) / 3. * timeout / nprocs)
        if pool_timeout < 100:
            pool_timeout = 100

        logger.info('Overseer timeout after %i second(s)' % pool_timeout)
        logger.info('Chunksize: %i' % chunksize)

        try:
            yield pool.map_async(
                _pay_worker, workers,
                chunksize=chunksize, callback=callback).get(pool_timeout)
        except multiprocessing.TimeoutError:
            logger.error('Overseer fell asleep. Fire everyone!')
            pool.terminate()
        except KeyboardInterrupt:
            logger.error('Got Ctrl + C')
            traceback.print_exc()
            pool.terminate()
        else:
            pool.close()
            pool.join()
            # reset process counter for tqdm progressbar
            multiprocessing.process._current_process._counter = count(1)


def memshare_sparams(shared_params):
    """
    For each parameter in a list of Theano TensorSharedVariable
    we substitute the memory with a sharedctype using the
    multiprocessing library.

    The wrapped memory can then be used by other child processes
    thereby synchronising different instances of a model across
    processes (e.g. for multi cpu gradient descent using single cpu
    Theano code).

    Parameters
    ----------
    shared_params : list
        of :class:`theano.tensor.sharedvar.TensorSharedVariable`

    Returns:
    --------

    memshared_instances : list
        of :class:`multiprocessing.sharedctypes.RawArray`
        list of sharedctypes (shared memory arrays) that point
        to the memory used by the current process's Theano variable.

    Notes:
    ------
    Modiefied from:
    https://github.com/JonathanRaiman/theano_lstm/blob/master/theano_lstm/shared_memory.py

        # define some theano function:
        myfunction = myfunction(20, 50, etc...)

        # wrap the memory of the Theano variables:
        memshared_instances = make_params_shared(myfunction.get_shared())

    Then you can use this memory in child processes
    (See usage of `borrow_memory`)
    """
    memshared_instances = []
    for param in shared_params:
        original = param.get_value(True, True)
        size = original.size
        shape = original.shape
        original.shape = size

        ctypes = multiprocessing.RawArray(
            'f' if original.dtype == num.float32 else 'd', original)
        wrapped = num.frombuffer(ctypes, dtype=original.dtype, count=size)
        wrapped.shape = shape

        param.set_value(wrapped, borrow=True)
        memshared_instances.append(ctypes)

    return memshared_instances


def borrow_memory(shared_param, memshared_instance):
    """
    Spawn different processes with the shared memory
    of your theano model's variables.
    Inputs:
    -------
    param: TensorSharedVariable : the Theano shared variable where
                                          shared memory should be used instead.
    memshared_instance : :class:`multiprocessing.RawArray`
        the memory shared across processes (e.g.from `memshare_sparams`)

    Notes:
    ------
    Modiefied from:
    https://github.com/JonathanRaiman/theano_lstm/blob/master/theano_lstm/shared_memory.py

    For each process in the target function run the theano_borrow_memory
    method on the parameters you want to have share memory across processes.
    In this example we have a model called "mymodel" with parameters stored in
    a list called "params". We loop through each theano shared variable and
    call `theano_borrow_memory` on it to share memory across processes.
        def spawn_model(path, wrapped_params):
            # prevent recompilation and arbitrary locks
            theano.config.reoptimize_unpickled_function = False
            theano.gof.compilelock.set_lock_status(False)
            # load your model from its pickled instance (from path)
            mymodel = MyModel.load(path)

            # for each parameter in your model
            # apply the borrow memory strategy to replace
            # the internal parameter's memory with the
            # across-process memory
            for param, memshared_instance in zip(
                    mymodel.params, memshared_instances):
                borrow_memory(param, memory)

            # acquire your dataset (either through some smart shared memory
            # or by reloading it for each process)
            dataset, dataset_labels = acquire_dataset()

            # then run your model forward in this process
            epochs = 20
            for epoch in range(epochs):
                model.update_fun(dataset, dataset_labels)
    See `borrow_all_memories` for list usage.
    """

    param_value = num.frombuffer(memshared_instance)
    param_value.shape = shared_param.get_value(True, True).shape
    shared_param.set_value(param_value, borrow=True)


def borrow_all_memories(params, memshared_instances):
    """
    Run theano_borrow_memory on a list of params and shared memory
    sharedctypes.
    Inputs:
    -------
    param  list<TensorSharedVariable>         : list of Theano shared variable where
                                                shared memory should be used instead.
    memory list<multiprocessing.sharedctypes> : list of memory shared across processes (e.g.
                                                from `wrap_params`)
    Outputs:
    --------
    None
    Usage:
    ------
    Same as `borrow_memory` but for lists of shared memories and
    theano variables. See `borrow_memory`
    """
    for param, memory_handler in zip(params, memory_handlers):
        borrow_memory(param, memory_handler)
