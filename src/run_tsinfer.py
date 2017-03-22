"""
Simple CLI to run tsinf on the command line.
"""
import sys
import os
import argparse

import numpy as np

# use the local copy of msprime in preference to the global one
sys.path.insert(1,os.path.join(sys.path[0],'..','msprime'))
sys.path.insert(1,os.path.join(sys.path[0],'..','tsinfer'))
import msprime
import tsinfer

def main():

    description = """Simple CLI wrapper for tsinfer
        msprime version: {}
        tsinfer version: {}""".format(msprime.__version__, tsinfer.__version__)
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--verbosity', '-v', action='count', default=0)
    parser.add_argument(
        "samples",
        help="The observed haplotypes as a numpy array file")
    parser.add_argument(
        "positions",
        help="The positions of sites as a numpy array file")
    parser.add_argument(
        "output",
        help="The path to write the output file to")
    parser.add_argument(
        "-l", "--length", default=None, type=int,
        help="The total sequence length")
    parser.add_argument(
        "-r", "--recombination-rate", default=1, type=float,
        help="The scaled recombination rate.")
    parser.add_argument(
        "-e", "--error-probability", default=0, type=float,
        help="The probablity of observing an error")
    parser.add_argument(
        "-t", "--threads", default=1, type=int,
        help="The number of worker threads to use")

    args = parser.parse_args()
    S = np.load(args.samples)
    pos = np.load(args.positions)
    panel = tsinfer.ReferencePanel(
        S, pos, args.length, args.recombination_rate, ancestor_error=0,
        sample_error=args.error_probability)
    P, mutations = panel.infer_paths(num_workers=args.threads)
    ts_new = panel.convert_records(P, mutations)
    ts_simplified = ts_new.simplify()
    ts_simplified.dump(args.output)

if __name__ == "__main__":
    main()
