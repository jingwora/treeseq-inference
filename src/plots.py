"""
Code to run simulations, inference methods and generate all plots
in the paper.
"""

import argparse
import logging
import os.path
import shutil
import sys
import time
import glob
import collections
import itertools
import tempfile
import subprocess
import filecmp
import random

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as pyplot
import pandas as pd

# import the local copy of msprime in preference to the global one
curr_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(1,os.path.join(curr_dir,'..','msprime'))
import msprime
import msprime_extras
import msprime_fastARG
import msprime_ARGweaver
import tsinf
import ARG_metrics

fastARG_executable = os.path.join(curr_dir,'..','fastARG','fastARG')
ARGweaver_executable = os.path.join(curr_dir,'..','argweaver','bin','arg-sample')
#monkey-patch write.nexus into msprime
msprime.TreeSequence.write_nexus_trees = msprime_extras.write_nexus_trees

if sys.version_info[0] < 3:
    raise Exception("Python 3 only")

def expand_grid(data_dict):
    """from last example in http://pandas.pydata.org/pandas-docs/stable/cookbook.html"""
    rows = itertools.product(*data_dict.values())
    return pd.DataFrame.from_records(rows, columns=data_dict.keys())

def add_columns(dataframe, colnames):
    """
    add the named columns to a data frame unless they already exist
    """
    for cn in colnames:
        if cn not in dataframe.columns:
            dataframe[cn]=np.NaN

def make_errors(v, p):
    """
    Flip each bit with probability p.
    """
    m = v.shape[0]
    mask = np.random.random(m) < p
    return np.logical_xor(v.astype(bool), mask).astype(int)

def generate_samples(ts, error_p):
    """
    Returns samples with a bits flipped with a specified probability.

    Rejects any variants that result in a fixed column.
    """
    S = np.zeros((ts.sample_size, ts.num_mutations), dtype="u1")
    for variant in ts.variants():
        done = False
        # Reject any columns that have no 1s or no zeros
        while not done:
            S[:,variant.index] = make_errors(variant.genotypes, error_p)
            s = np.sum(S[:, variant.index])
            done = 0 < s < ts.sample_size
    return S

def msprime_name(n, Ne, l, rho, mu, genealogy_seed, mut_seed, directory=None):
    """
    Create a filename for saving an msprime simulation (without extension)
    Other functions add error rates and/or a subsample sizes to the name
    """
    #format mut rate & recomb rate to print non-exponential notation without
    # trailing zeroes 12 dp should be ample for these rates
    rho = "{:.12f}".format(float(rho)).rstrip('0')
    mu = "{:.12f}".format(float(mu)).rstrip('0')
    file = "msprime-n{}_Ne{}_l{}_rho{}_mu{}-gs{}_ms{}".format(int(n), float(Ne), int(l), rho, \
        mu, int(genealogy_seed), int(mut_seed))
    if directory is None:
        return file
    else:
        return os.path.join(directory,file)

def add_error_param_to_name(sim_name, error_rate):
    """
    Append the error param to the msprime simulation filename.
    Only relevant for files downstream of the step where sequence error is added
    """
    if sim_name.endswith("+") or sim_name.endswith("-"):
        #this is the first param
        return sim_name + "_err{}".format(float(error_rate))
    else:
        #this is the first param
        return sim_name + "err{}".format(float(error_rate))

def add_subsample_param_to_name(sim_name, subsample_size):
    """
    Mark a filename as containing only a subset of the samples of the full sim
    Can be used on msprime output files but also e.g. msinfer output files
    """
    if sim_name.endswith("+") or sim_name.endswith("-"):
        #this is the first param
        return sim_basename + "max{}".format(int(subsample_size))
    else:
        return sim_basename + "_max{}".format(int(subsample_size))

def construct_fastarg_name(sim_name, seed, directory=None):
    """
    Returns a fastARG inference filename (without file extension),
    based on a simulation name
    """
    d,f = os.path.split(sim_name)
    return os.path.join(d,'+'.join(['fastarg', f, "fs"+str(int(seed))]))

def construct_argweaver_name(sim_name, seed, iteration_number=None):
    """
    Returns an ARGweaver inference filename (without file extension),
    based on a simulation name. The iteration number (used in .smc and .nex output)
    is usually added by the ARGweaver `arg-sample` program,
    in the format .10, .20, etc. (we append an 'i' too, giving
    'i.10', 'i.100', etc
    """
    d,f = os.path.split(sim_name)
    suffix = "ws"+str(int(seed))
    if iteration_number is not None:
        suffix += "_i."+ str(int(iteration_number))
    return os.path.join(d,'+'.join(['aweaver', f, suffix]))

def construct_msinfer_name(sim_name):
    """
    Returns an MSprime Li & Stevens inference filename.
    If the file is a subset of the original, this can be added to the
    basename later using the add_subsample_param_to_name() routine.
    """
    d,f = os.path.split(sim_name)
    return os.path.join(d,'+'.join(['msinfer', f, ""]))

def get_seeds(n, seed=123, upper=1e7):
    """
    returns a set of n unique seeds suitable for seeding RNGs
    """
    np.random.seed()
    seeds = np.random.randint(upper, size=n)
    seeds = np.unique(seeds)
    while len(seeds) != n:
        seeds = np.append(np.random.randint(upper,size = n-len(seeds)))
        seeds = np.unique(seeds)
    return seeds

def time_cmd(cmd, stdout):
    """
    Runs the specified command line (a list suitable for subprocess.call)
    and writes the stdout to the specified file object.
    """
    if sys.platform == 'darwin':
        #on OS X, install gtime using `brew install gnu-time`
        time_cmd = "/usr/local/bin/gtime"
    else:
        time_cmd = "/usr/bin/time"
    full_cmd = [time_cmd, "-f%M %S %U"] + cmd
    with tempfile.TemporaryFile() as stderr:
        exit_status = subprocess.call(full_cmd, stderr=stderr, stdout=stdout)
        stderr.seek(0)
        if exit_status != 0:
            raise ValueError(
                "Error running '{}': status={}:stderr{}".format(
                    " ".join(cmd), exit_status, stderr.read()))

        split = stderr.readlines()[-1].split()
        # From the time man page:
        # M: Maximum resident set size of the process during its lifetime,
        #    in Kilobytes.
        # S: Total number of CPU-seconds used by the system on behalf of
        #    the process (in kernel mode), in seconds.
        # U: Total number of CPU-seconds that the process used directly
        #    (in user mode), in seconds.
        max_memory = int(split[0]) * 1024
        system_time = float(split[1])
        user_time = float(split[2])
    return user_time + system_time, max_memory


class Figure(object):
    """
    Superclass of all figures. Each figure depends on a dataset.
    """
    name = None
    """
    Each figure has a unique name. This is used as the identifier and the
    file name for the output plots.
    """

    def plot(self):
        raise NotImplementedError()

class Dataset(object):
    """
    A dataset is some collection of simulations which are run using
    the generate() method and stored in the raw_data_path directory.
    The process() method then processes the raw data and outputs
    the results into the data file.
    """
    name = None
    """
    Each dataset has a unique name. This is used as the prefix for the data
    file and raw_data_dir directory. Within this, replicate instances of datasets
    (each with a different RNG seed) are saved under the seed number
    """

    data_dir = "data"

    def __init__(self, data_file=None, reps=None, seed=None):
        """
        Creates initial names for a data file & data dir.

        A data_file can be passed which should contain a '+'
        followed by param1=value1_param2=value2, etc.
        Otherwise, parameters should be passed as **params

        If data file already exists, read it in to self.data,
        otherwise set self.data to None, which flags up that
        we need to generate one.

        Return dict of params (either as set or from the data file name)
        """
        if data_file is None and reps is None and seed is None:
            # look for an existing data file in the data_dir
            potential_data_files = glob.glob(os.path.join(self.data_dir,"{}+*.csv".format(self.name)))
            try:
                data_file = max(potential_data_files, key = os.path.getctime)
                if len(potential_data_files) > 1:
                    logging.info(("More than one data file for {}. " +
                        "Picking the most recent {}").format(self.name, data_file))
            except:
                sys.exit("No data file found, and no parameters specified")

        if data_file is not None:
            self.data = pd.read_csv(data_file)
            assert data_file.endswith(".csv")
            assert data_file.rfind("+") != -1
            #extract params from the filename
            param_string = data_file[(data_file.rfind("+")+1):-4]
            run_params = {p.split("=",1)[0]:p.split("=",1)[1] for p in param_string.split("_")}
        else:
            self.data = None
            run_params = {'reps':reps, 'seed':seed}

        self.run_id = "_".join([k+"="+str(run_params[k]) for k in sorted(run_params.keys())])
        self.data_file = os.path.abspath(os.path.join(self.data_dir,
            "{}+{}.csv".format(self.name, self.run_id)))

        self.raw_data_dir = os.path.join(self.data_dir, "raw__NOBACKUP__", self.name, str(self.run_id))
        if not os.path.exists(self.raw_data_dir):
            logging.info("Making raw data dir {}".format(self.raw_data_dir))
        os.makedirs(self.raw_data_dir, exist_ok=True)

        return(run_params)

    @staticmethod
    def set_default_run_params(param_dict, default_dict):
        for key, val in default_dict.items():
            if key in param_dict and param_dict[key] is None:
                param_dict[key] = val

    def setup(self):
        # clear out any old simulations to avoid confusion.
        if os.path.exists(self.simulations_dir):
            shutil.rmtree(self.simulations_dir)
        os.makedirs(self.simulations_dir)
        for s in range(max(self.data.sim)+1):
            sim = self.data[self.data.sim == s]
            ts, fn = self.single_simulation(
                int(pd.unique(sim.sample_size)),
                int(pd.unique(sim.Ne)),
                int(pd.unique(sim.length)),
                float(pd.unique(sim.recombination_rate)),
                float(pd.unique(sim.mutation_rate)),
                int(pd.unique(sim.seed)),
                int(pd.unique(sim.seed))) #same mutation_seed as genealogy_seed
            with open(fn +".nex", "w+") as out:
                ts.write_nexus_trees(out)
            self.save_variant_matrices(ts, fn, pd.unique(sim.error_rate))

    def generate(self):
        """
        TODO abstract out the common functionality across different datasets.
        We should have a main loop here in common with all the datasets
        and write the code to run the various tools exactly once. We can have
        calls into specific things we need to have in subclasses, but we should
        definitely not have duplication of big important chunks of code.

        This is currently the version of generate() from the
        MetricsByMutationRateDataset.
        """
        results = ['CPUtime','memory']
        tool_cols = {t:["_".join([t,rc]) for rc in results] for t in self.tools}
        # We need to store more information in the case of ARGweaver, since
        # each ARGweaver run produces a whole set of iterations. We join these
        # together in a single column
        if "ARGweaver" in tool_cols:
            tool_cols["ARGweaver"].append("ARGweaver_iterations")
        for tool in self.tools:
            add_columns(self.data, tool_cols[tool])

        for i in self.data.index:
            d = self.data.iloc[i]
            sim_fn = msprime_name(
                d.sample_size, d.Ne, d.length, d.recombination_rate,
                d.mutation_rate, d.seed, d.seed, self.simulations_dir)
            err_fn = add_error_param_to_name(sim_fn, d.error_rate)
            inference_seed = int(d.seed + i)
            self.data.loc[i, 'inference_seed'] = inference_seed
            for tool, result_cols in sorted(tool_cols.items()):
                if tool == 'msinfer':
                    infile = err_fn + ".npy"
                    logging.info("generating tsinf inference for mu = {}".format(
                        d.mutation_rate))
                    logging.debug(
                        "reading: variant matrix {} for msprime inference".format(infile))
                    S = np.load(infile)
                    infile = err_fn + ".pos.npy"
                    logging.debug(
                        "reading: positions {} for msprime inference".format(infile))
                    pos = np.load(infile)
                    out_fn = construct_msinfer_name(err_fn)
                    inferred_ts, time, memory = self.run_tsinf(
                        S, d.length, pos, 4 * d.recombination_rate * d.Ne)
                    with open(out_fn +".nex", "w+") as out:
                        inferred_ts.write_nexus_trees(out)
                    self.data.loc[i, result_cols] = (time, memory)
                elif tool == 'fastARG':
                    infile = err_fn + ".hap"
                    out_fn = construct_fastarg_name(err_fn, inference_seed)
                    logging.info("generating fastARG inference for mu = {}".format(
                        d.mutation_rate))
                    logging.debug("reading: {} for fastARG inference".format(infile))
                    inferred_ts, time, memory = self.run_fastarg(infile, d.length, inference_seed)
                    with open(out_fn +".nex", "w+") as out:
                        inferred_ts.write_nexus_trees(out)
                    self.data.loc[i, result_cols] = (time, memory)
                elif tool == 'ARGweaver':
                    infile = err_fn + ".sites"
                    out_fn = construct_argweaver_name(err_fn, inference_seed)
                    logging.info(
                        "generating ARGweaver inference for mu = {}".format(d.mutation_rate))
                    logging.debug("reading: {} for ARGweaver inference".format(infile))
                    iteration_ids, stats_file, time, memory = self.run_argweaver(
                        infile, d.Ne, d.recombination_rate, d.mutation_rate,
                        out_fn, inference_seed, d.aw_n_out_samples,
                        d.aw_iter_out_freq, d.aw_burnin_iters)
                    #now must convert all of the .smc files to .nex format
                    for it in iteration_ids:
                        base = construct_argweaver_name(err_fn, inference_seed, it)
                        with open(base+".nex", "w+") as out:
                            msprime_ARGweaver.ARGweaver_smc_to_nexus(
                                base+".smc.gz", out, zero_based_tip_numbers=False)
                    self.data.loc[i, result_cols] = (time, memory, ",".join(iteration_ids))
                else:
                    raise KeyError

            # Save each row so we can use the information while it's being built
            self.data.to_csv(self.data_file)



    def process(self):
        """
        processes the inferred ARGs
        """
        raise NotImplementedError()

    def single_simulation(self, n, Ne, l, rho, mu, seed, mut_seed=None, subsample=None):
        """
        The standard way to run one msprime simulation for a set of parameter
        values. Saves the output to an .hdf5 file, and also saves variant files
        for use in fastARG (a .hap file, in the format specified by
        https://github.com/lh3/fastARG#input-format) ARGweaver (a .sites file:
        http://mdrasmus.github.io/argweaver/doc/#sec-file-sites) msinfer
        (currently a numpy array containing the variant matrix)

        mutation_seed is not yet implemented, but should allow the same
        ancestry to be simulated (if the same genealogy_seed is given) but have
        different mutations thrown onto the trees (even with different
        mutation_rates)

        Returns a tuple of treesequence, filename (without file type extension)
        """
        logging.info(
            "Running simulation for n = {}, l = {}, rho={}, mu = {} seed={}".format(
                n, l, rho, mu, seed))
        # Since we want to have a finite site model, we force the recombination map
        # to have exactly l loci with a recombination rate of rho between them.
        recombination_map = msprime.RecombinationMap.uniform_map(l, rho, l)
        # We need to rejection sample any instances that we can't discretise under
        # the current model. The simplest way to do this is to have a local RNG
        # which we seed with the specified seed.
        rng = random.Random(seed)
        # TODO replace this with a proper finite site mutation model in msprime.
        done = False
        while not done:
            sim_seed = rng.randint(1, 2**31)
            ts = msprime.simulate(
                n, Ne, recombination_map=recombination_map, mutation_rate=mu,
                random_seed=sim_seed)
            try:
                ts = msprime_extras.discretise_mutations(ts)
                done = True
            except ValueError as ve:
                logging.info("Rejecting simulation: seed={}: {}".format(seed, ve))
        # Here we might want to iterate over mutation rates for the same
        # genealogy, setting a different mut_seed so that we can see for
        # ourselves the effect of mutation rate variation on a single topology
        # but for the moment, we don't bother, and simply write
        # mut_seed==genealogy_seed
        sim_fn = msprime_name(n, Ne, l, rho, mu, seed, seed, self.simulations_dir)
        logging.debug("writing {}.hdf5".format(sim_fn))
        ts.dump(sim_fn+".hdf5", zlib_compression=True)
        if subsample is not None:
            ts = ts.simplify(list(range(subsample)))
            sim_fn = add_subsample_param_to_name(sim_fn, subsample)
            ts.dump(sim_fn +".hdf5", zlib_compression=True)
        return ts, sim_fn

    @staticmethod
    def save_variant_matrices(ts, fname, error_rates):
        for error_rate in error_rates:
            S = generate_samples(ts, error_rate)
            err_filename = add_error_param_to_name(fname, error_rate)
            outfile = err_filename + ".npy"
            logging.debug("writing variant matrix to {} for msinfer".format(outfile))
            np.save(outfile, S)
            outfile = err_filename + ".pos.npy"
            pos = np.array([v.position for v in ts.variants()])
            logging.debug("writing variant positions to {} for msinfer".format(outfile))
            np.save(outfile, pos)
            assert all(p.is_integer() for p in pos), \
                "Variant positions are not all integers in {}".format()
            logging.debug("writing variant matrix to {}.hap for fastARG".format(err_filename))
            with open(err_filename+".hap", "w+") as fastarg_in:
                msprime_fastARG.variant_matrix_to_fastARG_in(np.transpose(S), pos, fastarg_in)
            logging.debug("writing variant matrix to {}.sites for ARGweaver".format(err_filename))
            with open(err_filename+".sites", "w+") as argweaver_in:
                msprime_ARGweaver.variant_matrix_to_ARGweaver_in(np.transpose(S), pos,
                    ts.get_sequence_length(), argweaver_in)

    @staticmethod
    def run_tsinf(S, sequence_length, sites, rho, num_workers=1):
        before = time.clock()
        panel = tsinf.ReferencePanel(S, sites, sequence_length)
        P = panel.infer_paths(rho, num_workers=num_workers)
        ts_new = panel.convert_records(P)
        ts_simplified = ts_new.simplify()
        cpu_time = time.clock() - before
        memory_use = 0 #To DO
        return ts_simplified, cpu_time, memory_use

    @staticmethod
    def run_fastarg(file_name, seq_length, seed):
        with tempfile.NamedTemporaryFile("w+") as fa_out, \
                tempfile.NamedTemporaryFile("w+") as tree, \
                tempfile.NamedTemporaryFile("w+") as muts, \
                tempfile.NamedTemporaryFile("w+") as fa_revised:
            cmd = msprime_fastARG.get_cmd(fastARG_executable, file_name, seed)
            cpu_time, memory_use = time_cmd(cmd, fa_out)
            logging.debug("ran fastarg [{} s]: '{}'".format(cpu_time, cmd))
            var_pos = msprime_fastARG.variant_positions_from_fastARGin_name(file_name)
            root_seq = msprime_fastARG.fastARG_root_seq(fa_out)
            msprime_fastARG.fastARG_out_to_msprime_txts(
                    fa_out, var_pos, tree, muts, seq_len=seq_length)
            inferred_ts = msprime_fastARG.msprime_txts_to_fastARG_in_revised(
                    tree, muts, root_seq, fa_revised, simplify=True)
            try:
                assert filecmp.cmp(file_name, fa_revised.name, shallow=False), \
                    "{} and {} differ".format(file_name, fa_revised.name)
            except AssertionError:
                warn("File '{}' copied to 'bad.hap' for debugging".format(
                    fa_revised.name), file=sys.stderr)
                shutil.copyfile(fa_revised.name, os.path.join('bad.hap'))
                raise
            return inferred_ts, cpu_time, memory_use

    @staticmethod
    def run_argweaver(sites_file, Ne, recombination_rate,
        mutation_rate, path_prefix, seed,
        n_samples, sampling_freq, burnin_iterations=0):
        """
        this produces a whole load of .smc files labelled <path_prefix>i.0.smc,
        <path_prefix>i.10.smc, etc. the iteration numbers ('i.0', 'i.10', etc)
        are returned by this function

        TO DO: if verbosity < 0 (logging level == warning) we should set quiet = TRUE

        """
        new_prefix = path_prefix + "_i" #we append a '_i' to mark iteration number
        before = time.clock()
        msprime_ARGweaver.run_ARGweaver(Ne=Ne,
            mut_rate     = mutation_rate,
            recomb_rate  = recombination_rate,
            executable   = ARGweaver_executable,
            ARGweaver_in = sites_file,
            sample_step  = sampling_freq,
            iterations   = sampling_freq * (n_samples-1),
            #e.g. for 3 samples at sampling_freq 10, run for 10*(3-1) iterations
            #which gives 3 samples: t=0, t=10, & t=20
            rand_seed    = int(seed),
            burn_in      = int(burnin_iterations),
            quiet        = True,
            out_prefix   = new_prefix)
        cpu_time = time.clock() - before
        memory_use = 0 #To DO
        #here we need to look for the .smc files output by argweaver
        smc_prefix = new_prefix + "." #the arg-sample program adds .iteration_num
        iterations = [f[len(smc_prefix):-7] for f in glob.glob(smc_prefix + "*" + ".smc.gz")]
        new_stats_file_name = path_prefix+".stats"
        os.rename(new_prefix + ".stats", new_stats_file_name)
        #cannot translate these to msprime ts objects, as smc2arg does not work
        #see https://github.com/mdrasmus/argweaver/issues/20
        return iterations, new_stats_file_name, cpu_time, memory_use

class NumRecordsBySampleSizeDataset(Dataset):
    """
    Information on the number of coalescence records inferred by tsinf
    and FastARG for various sample sizes, under 3 different error rates
    """
    name = "num_records_by_sample_size"
    tools = ["fastARG", "msinfer"]

    def __init__(self, **params):
        """
        Everything is done via a Data Frame which contains the initial params and is then used to
        store the output values.
        """
        self.set_default_run_params(params, {'seed':123, 'reps':10})
        setup_params = super().__init__(**params)
        self.simulations_dir = os.path.join(self.raw_data_dir, "simulations")
        self.seed=int(setup_params['seed'])
        if self.data is None: #need to create the data file
            #set up a pd Data Frame containing all the sim params
            params = collections.OrderedDict()
            params['sample_size']       = np.linspace(10, 500, num=10).astype(int)
            params['Ne']                 = 5000,
            params['length']             = 50000,
            params['recombination_rate'] = 2.5e-8,
            params['mutation_rate']      = 1.5e-8,
            params['replicate']         = np.arange(int(setup_params['reps']))
            sim_params = list(params.keys())
            #now add some other params, where multiple vals exists for one simulation
            params['error_rate']         = [0, 0.1, 0.01]

            #all combinations of params in a DataFrame
            self.data = expand_grid(params)
            #assign a unique index for each simulation
            self.data['sim'] = pd.Categorical(self.data[sim_params].astype(str).apply("".join, 1)).codes
            #set unique seeds for each sim
            self.data['seed'] = get_seeds(max(self.data.sim)+1, self.seed)[self.data.sim]
            #now save it for future ref
            self.data.to_csv(self.data_file)

    def generate(self):
        # This is effectively not implemented.
        raise NotImplementedError()




class MetricsByMutationRateDataset(Dataset):
    """
    Dataset for Figure 1
    Accuracy of ARG inference (measured by various statistics)
    tending to fully accurate as mutation rate increases
    """
    name = "metrics_by_mutation_rate"
    tools = ["fastARG","msinfer","ARGweaver"]

    def __init__(self, **params):
        """
        Everything is done via a Data Frame which contains the initial params and is
        then used to store the output values.
        """
        self.set_default_run_params(params, {'seed':13, 'reps':10})
        setup_params = super().__init__(**params)
        self.simulations_dir = os.path.join(self.raw_data_dir, "simulations")
        self.seed=int(setup_params['seed'])
        if self.data is None: #need to create the data file
            #set up a pd Data Frame containing all the params
            params = collections.OrderedDict()
            params['sample_size']         = 12,
            params['Ne']                  = 5000,
            params['length']              = 50000,
            params['recombination_rate']  = 2.5e-8,
            params['mutation_rate']       = np.logspace(-8, -5, num=10)[:-1] * 1.5
            params['replicate']           = np.arange(int(setup_params['reps']))
            sim_params = list(params.keys())
            #now add some other params, where multiple vals exists for one simulation
            params['error_rate']          = [0, 0.01, 0.1]
            #RNG seed used in inference methods - will be filled out in the generate() step
            params['inference_seed']     = np.nan,
            ## argweaver params: aw_n_out_samples will be produced, every argweaver_iter_out_freq
            params['aw_burnin_iters']    = 5000,
            params['aw_n_out_samples']   = 100,
            params['aw_iter_out_freq']   = 10,
            ## msinfer params: number of times to randomly resolve into bifurcating (binary) trees
            params['msinfer_biforce_reps']= 20,

            #all combinations of params in a DataFrame
            self.data = expand_grid(params)
            #assign a unique index for each simulation
            self.data['sim'] = pd.Categorical(self.data[sim_params].astype(str).apply("".join, 1)).codes
            #set unique seeds for each sim
            self.data['seed'] = get_seeds(max(self.data.sim)+1, self.seed)[self.data.sim]
            #now save it for future ref
            self.data.to_csv(self.data_file)

    def process(self):
        """
        Extracts metrics from the .nex files using R, and saves the results
        in the csv file under fastARG_RFunrooted, etc etc.
        The msinfer routine has two possible sets of stats: for nonbinary trees
        and for randomly resolved strictly bifurcating trees (with a random seed given)
        These are stored in 'msipoly_RFunrooted' and
        'msibifu_RFunrooted', and the random seed used (as passed to R via
        genome_trees_dist_forcebin_b) is stored in 'msibifu_seed'
        """

        for i in self.data.index:
            d = self.data.iloc[i]
            sim_fn=msprime_name(d.sample_size, d.Ne, d.length, d.recombination_rate,
                                d.mutation_rate, d.seed, d.seed, self.simulations_dir)
            base_fn=add_error_param_to_name(sim_fn, d.error_rate)
            polytomy_resolution_seed = d.inference_seed #hack: use same seed as in inference step
            toolfiles = {
                "fastARG":construct_fastarg_name(base_fn, d.inference_seed)+".nex",
                "ARGweaver":{'nexus':construct_argweaver_name(base_fn, d.inference_seed, it)+".nex" \
                    for it in d['ARGweaver_iterations'].split(",")},
                "MSIpoly":construct_msinfer_name(base_fn)+".nex",
                "MSIbifu":{'nexus':construct_msinfer_name(base_fn)+".nex",
                           'reps':d.msinfer_biforce_reps,
                           'seed':polytomy_resolution_seed}
            }
            logging.info("Processing ARG to extract metrics: mu = {}, err = {}.".format(
                d.mutation_rate, d.error_rate))
            m = ARG_metrics.get_ARG_metrics(sim_fn + ".nex", **toolfiles)
            self.data.loc[i,"MSIbifu_seed"] = polytomy_resolution_seed
            #Now add all the metrics to the data file
            for prefix, metrics in m.items():
                colnames = ["_".join([prefix,k]) for k in sorted(metrics.keys())]
                add_columns(self.data, colnames)
                self.data.loc[i, colnames] = metrics.values()

             # Save each row so we can use the information while it's being built
            self.data.to_csv(self.data_file)

class SampleSizeEffectOnSubsetDataset(Dataset):
    """
    Dataset for Figure 3
    Dataset testing the effect on a subset of samples when extra genomes are
    added to the dataset used for inference. The hope is that the extra samples
    allowed by our msinference method will more than compensate for biases in the
    inference method over more statistically rigorous methods (e.g. ARGweaver).

    We should expect this to be the case, as the benefit of adding more
    intermediate branches to trees (e.g. to halt long-branch attraction
    artifacts) is well documented in the phylogenetic literature, under
    the general heading of 'taxon sampling'.

    Method: take a large simulated dataset (thousands?), with fixed, realistic
    mutation and recombination rates, over a large span of genome. Use the msprime
    simplify() function to cut it down to a subset of n genomes (n ~ 10 or 20).
    Call this subset S_n_base. Run all the inference methods on this base subset
    to get inference outputs (eventually saved as .nex files). Then take larger
    and larger subsets of the original simulation - S_N for N=20, 40, 100, etc. -
    and run msinfer (but not the other methods) on these larger subsets, ensuring
    that after each inference attempt, the resulting TreeSequence is subset
    (simplify()ed) back down to cover only the first n samples. Use ARGmetrics to
    compare these secondarily-simplified ARGs with the true (original, simulated)
    S_n subset. We would hope that the ARG accuracy metrics tend to 0 as the msinfer
    subset size N goes up.
    """
    name = "sample_size_effect_on_subset"

    def __init__(self, **params):
        """
        Note that here we do *not* want to try all combinations of subset size
        with all inference tools. We only want to try
        """

    def process(self):
        """
        Extracts metrics from the .nex files using R, and saves the results
        in the csv file under fastARG_RFunrooted, etc etc.
        The msinfer routine has two possible sets of stats: for nonbinary trees
        and for randomly resolved strictly bifurcating trees (with a random seed given)
        These are stored in 'msipoly_RFunrooted' and
        'msibifu_RFunrooted', and the random seed used (as passed to R via
        genome_trees_dist_forcebin_b) is stored in 'msibifu_seed'
        """

def run_setup(cls, extra_args):
    set_args = {k:v for k,v in extra_args.items() if v is not None}
    set_text = " with extra arguments {}".format(set_args) if set_args else ""
    logging.info("Setting up base data for {}{}".format(cls.name, set_text))
    f = cls(**extra_args)
    f.setup()

def run_generate(cls, extra_args):
    set_args = {k:v for k,v in extra_args.items() if v is not None}
    set_text = " from {}".format(set_args) if set_args else ""
    logging.info("Generating {}".format(cls.name))
    f = cls(**extra_args)
    f.generate()


def run_process(cls, extra_args):
    set_args = {k:v for k,v in extra_args.items() if v is not None}
    set_text = " from {}".format(set_args) if set_args else ""
    logging.info("Processing {}".format(cls.name))
    f = cls(**extra_args)
    f.process()


def run_plot(cls, extra_args):
    f = cls(**extra_args)
    f.plot()


def main():
    datasets = [
        NumRecordsBySampleSizeDataset,
        MetricsByMutationRateDataset,
    ]
    figures = [
    ]
    name_map = dict([(d.name, d) for d in datasets + figures])
    parser = argparse.ArgumentParser(
        description= "Set up base data, generate inferred datasets, process datasets and plot figures.")
    parser.add_argument('--verbosity', '-v', action='count', default=0)
    subparsers = parser.add_subparsers()
    subparsers.required = True
    subparsers.dest = 'command'

    subparser = subparsers.add_parser('setup')
    # TODO: something like this will be useful to set up smaller runs for
    # testing purposes and to control the number of processes used.
    # subparser.add_argument(
    #     "--processes", '-p', help="number of processes",
    #     type=int, default=1)
    subparser.add_argument(
        'name', metavar='NAME', type=str, nargs=1,
        help='the dataset identifier')
    subparser.add_argument(
         '--reps', '-r', type=int, help="number of replicates")
    subparser.add_argument(
         '--seed', '-s', type=int, help="use a non-default RNG seed")
    subparser.set_defaults(func=run_setup)

    subparser = subparsers.add_parser('generate')
    subparser.add_argument(
        'name', metavar='NAME', type=str, nargs=1,
        help='the dataset identifier')
    subparser.add_argument(
         '--data-file', '-f', type=str,
         help="which CSV file to use for existing data, if not the default", )
    subparser.set_defaults(func=run_generate)

    subparser = subparsers.add_parser('process')
    subparser.add_argument(
        'name', metavar='NAME', type=str, nargs=1,
        help='the dataset identifier')
    subparser.add_argument(
         '--data-file', '-f', type=str,
         help="which CSV file to use for existing data, if not the default")
    subparser.set_defaults(func=run_process)

    subparser = subparsers.add_parser('figure')
    subparser.add_argument(
        'name', metavar='NAME', type=str, nargs=1,
        help='the figure identifier')
    subparser.set_defaults(func=run_plot)

    args = parser.parse_args()
    log_level = logging.WARNING
    if args.verbosity == 1:
        log_level = logging.INFO
    if args.verbosity >= 2:
        log_level = logging.DEBUG
    logging.basicConfig(
        format='%(asctime)s %(message)s', level=log_level, stream=sys.stdout)

    base_args = ['name','verbosity','command','func']
    #pass in any extra args to the func, e.g. seed, reps
    extra_args = {k:v for k,v in vars(args).items() if k not in base_args}
    k = args.name[0]
    if k == "all":
        classes = datasets
        if args.func == run_plot:
            classes = figures
        for name, cls in name_map.items():
            if cls in classes:
                args.func(cls, extra_args)
    else:
        cls = name_map[k]
        args.func(cls, extra_args)

if __name__ == "__main__":
    main()
