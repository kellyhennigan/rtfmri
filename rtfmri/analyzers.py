"""Thread-based objects that analyze data as it arrives in the queues."""
from __future__ import print_function, division
from Queue import Empty
from time import sleep

import numpy as np

from nipy.algorithms.registration import HistogramRegistration

from .queuemanagers import Finder


class MotionAnalyzer(Finder):
    """Compute real-time motion statistics for quality control."""

    # Ultimately we'll want to make this more extensible.
    # Because of how the queues work, you won't be able to run multiple
    # analyzers at the same time. So if you wanted to, say, run some
    # real-time GLM analysis and have real-time motion QA, you can't
    # just have those as two separate objects.
    # But it should work to be able to do that in an object-oriented
    # hierarchy sense, where you always get motion QA (and maybe other
    # stuff, like spike detection) and can optionally do more complicated
    # or domain-specific stuff on top of that.
    # For the time-being I'm not putting a ton of thought into that design
    # though, just because I'm not really sure exactly how you would want
    # it to look like.
    def __init__(self, scanner, result_q, skip_vols=4, interval=1):
        """Initialize the queue."""
        super(MotionAnalyzer, self).__init__(interval)
        self.scanner = scanner
        self.result_q = result_q
        self.skip_vols = skip_vols

        # The reference volume (first of each acquisition)
        self.ref_vol = None

        # The previous volume (for scan-to-scan motion)
        self.pre_vol = None

    def compute_registration(self, moving, fixed):
        """Estimate a linear, within-modality transform from moving to fixed.

        Parameters
        ----------
        moving : nibabel image object
            Image to be registered to the reference image.
        fixed : nibabel image object
            Reference image.

        Returns
        -------
        T : nipy Rigid object
            Rigid matrix giving transform from moving to fixed.

        """
        reg = HistogramRegistration(moving, fixed, interp="tri")
        T = reg.optimize("rigid")
        return T

    def compute_rms(self, T):
        """Compute root mean squared displacement of a transform matrix.

        Parameters
        ----------
        T : nipy Rigid object
            Transformation matrix.
        rms : scalar
            Root-mean-squared displacement corresponding to the transformation.

        Notes
        -----
        This is implemented as it is in FSL's mcflirt algorithm.
        See this technical report by Mark Jenkinson for the derivation:
        http://www.fmrib.ox.ac.uk/analysis/techrep/tr99mj1/tr99mj1/node3.html

        """
        T = T.as_affine() - np.eye(4)

        # Decompose the transformation
        A = T[:3, :3]
        t = T[:3, 3]

        # This is radius for our idealized head, which is a sphere
        R = 80

        # Compute the RMS displacemant
        rms = np.sqrt(R ** 2 / 5 * A.T.dot(A).trace() + t.T.dot(t))

        return rms

    def new_scanner_run(self, vol):
        """Compare vol to ref to see if there has been a new scanner run."""
        if self.ref_vol is None:
            return True

        for item in ["exam", "series", "acquisition"]:
            if vol[item] != self.ref_vol[item]:
                return True
        return False

    def run(self):
        """This function gets looped over repetedly while thread is alive."""
        while self.alive:
            try:
                vol = self.scanner.get_volume(timeout=1)
            except Empty:
                sleep(self.interval)
                continue

            # Check if we need to update the reference image
            if self.new_scanner_run(vol):
                self.ref_vol = vol
                self.pre_vol = vol

                # Put a dictionary of null results in the queue
                result = dict(rot_x=0, rot_y=0, rot_z=0,
                              trans_x=0, trans_y=0, trans_z=0,
                              rms_ref=0, rms_pre=0)
                vol.update(result)
                self.result_q.put(vol)
                continue

            # Compute the transformation to the reference image
            T_ref = self.compute_registration(vol, self.ref_vol)

            # Compute the transformation to the previous image
            # TODO Look out for memory leaks - it's possible that
            # we will need to actively free up the old pre vol
            T_pre = self.compute_registration(vol, self.pre_vol)

            # Update the previous image with the current image
            self.pre_vol = vol

            # Compute the summary information for both transformations
            rms_ref = self.compute_rms(T_ref)
            rms_pre = self.compute_rms(T_pre)

            # Put the summary information into the result queue
            rot_x, rot_y, rot_z = T_ref.rotation
            trans_x, trans_y, trans_z = T_ref.translation
            result = dict(rot_x=rot_x, rot_y=rot_y, rot_z=rot_z,
                          trans_x=trans_x, trans_y=trans_y, trans_z=trans_z,
                          rms_ref=rms_ref, rms_pre=rms_pre)
            vol.update(result)
            self.result_q.put(vol)