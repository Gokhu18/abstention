from __future__ import division, print_function
import numpy as np
from .calibration import (inverse_softmax, get_hard_preds,
                          map_to_softmax_format_if_approrpiate)
from scipy import linalg


class AbstractImbalanceAdapterFunc(object):

    def __call__(self, unadapted_posterior_probs):
        raise NotImplementedError()


class PriorShiftAdapterFunc(AbstractImbalanceAdapterFunc):

    def __init__(self, multipliers, calibrator_func=lambda x: x):
        self.multipliers = multipliers
        self.calibrator_func = calibrator_func

    def __call__(self, unadapted_posterior_probs):

        #if supplied probs are in binary format, convert to softmax format
        if (len(unadapted_posterior_probs.shape)==1
            or unadapted_posterior_probs.shape[1]==1):
            softmax_unadapted_posterior_probs = np.zeros(
                (len(unadapted_posterior_probs),2)) 
            softmax_unadapted_posterior_probs[:,0] =\
                unadapted_posterior_probs
            softmax_unadapted_posterior_probs[:,1] =\
                1-unadapted_posterior_probs
        else:
            softmax_unadapted_posterior_probs =\
                unadapted_posterior_probs

        softmax_unadapted_posterior_probs =\
            self.calibrator_func(softmax_unadapted_posterior_probs)

        adapted_posterior_probs_unnorm =(
            softmax_unadapted_posterior_probs*self.multipliers[None,:])
        adapted_posterior_probs = (
            adapted_posterior_probs_unnorm/
            np.sum(adapted_posterior_probs_unnorm,axis=-1)[:,None])

        #return to binary format if appropriate
        if (len(unadapted_posterior_probs.shape)==1
            or unadapted_posterior_probs.shape[1]==1):
            if (len(unadapted_posterior_probs.shape)==1):
                adapted_posterior_probs =\
                    adapted_posterior_probs[:,1] 
            else:
                if (unadapted_posterior_probs.shape[1]==1):
                    adapted_posterior_probs =\
                        adapted_posterior_probs[:,1:2] 

        return adapted_posterior_probs


class AbstractImbalanceAdapter(object):

    def __call__(self, valid_labels, tofit_initial_posterior_probs,
                       valid_posterior_probs):
        raise NotImplementedError()


class AbstractShiftWeightEstimator(object):
    
    # Should return the ratios of the weights for each class 
    def __call__(self, valid_labels,
                       tofit_initial_posterior_probs,
                       valid_posterior_probs):
        raise NotImplementedError()


class NoWeightShift(AbstractShiftWeightEstimator):

    def __call__(self, valid_labels, tofit_initial_posterior_probs,
                       valid_posterior_probs):
        return np.ones(valid_posterior_probs.shape[1])


class EMImbalanceAdapter(AbstractImbalanceAdapter):

    def __init__(self, verbose=False,
                       tolerance=1E-6,
                       max_iterations=100,
                       calibrator_factory=None,
                       initialization_weight_ratio=NoWeightShift()):
        self.verbose = verbose
        self.tolerance = tolerance
        self.calibrator_factory = calibrator_factory
        self.max_iterations = max_iterations
        self.initialization_weight_ratio = initialization_weight_ratio

    def __call__(self, valid_labels,
                       tofit_initial_posterior_probs,
                       valid_posterior_probs):

        softmax_valid_posterior_probs =\
            map_to_softmax_format_if_approrpiate(
                values=valid_posterior_probs)
        if (valid_labels is not None):
            softmax_valid_labels =\
                map_to_softmax_format_if_approrpiate(
                    values=valid_labels)
        else:
            softmax_valid_labels = None

        #if binary labels were provided, convert to softmax format
        # for consistency
        if (self.calibrator_factory is not None):
            assert valid_posterior_probs is not None 
            calibrator_func = self.calibrator_factory(
                valid_preacts=softmax_valid_posterior_probs,
                valid_labels=softmax_valid_labels,
                posterior_supplied=True) 
        else:
            calibrator_func = lambda x: x

        valid_posterior_probs = calibrator_func(valid_posterior_probs)
        #compute the class frequencies based on the posterior probs to ensure
        # that if the valid posterior probs are supplied for "to fit", then
        # no shift is estimated
        valid_class_freq = np.mean(valid_posterior_probs, axis=0)

        if (self.verbose):
            print("Original class freq", valid_class_freq)
       
        softmax_initial_posterior_probs =\
            calibrator_func(map_to_softmax_format_if_approrpiate(
                values=tofit_initial_posterior_probs))

        #initialization_weight_ratio is a method that can be used to
        # estimate the ratios between the label frequencies in the
        # validation set and the to_fit set; it can be used to obtain a
        # better initialization for the class frequencies
        #We normalize the frequencies to sum to 1 because methods like BBSE
        # are not guaranteed to return weights that give probs that are valid
        first_iter_class_freq = (
         valid_class_freq*self.initialization_weight_ratio(
            valid_labels = softmax_valid_labels,
            tofit_initial_posterior_probs = softmax_initial_posterior_probs,
            valid_posterior_probs = softmax_valid_posterior_probs))
        first_iter_class_freq = (first_iter_class_freq/
                                 np.sum(first_iter_class_freq))

        current_iter_class_freq = first_iter_class_freq
        current_iter_posterior_probs = softmax_initial_posterior_probs
        next_iter_class_freq = None
        next_iter_posterior_probs = None
        iter_number = 0
        while ((next_iter_class_freq is None
            or (np.sum(np.abs(next_iter_class_freq
                              -current_iter_class_freq)
                       > self.tolerance)))
            and iter_number < self.max_iterations):

            if (next_iter_class_freq is not None):
                current_iter_class_freq=next_iter_class_freq 
                current_iter_posterior_probs=next_iter_posterior_probs
            current_iter_posterior_probs_unnorm =(
                (softmax_initial_posterior_probs
                 *current_iter_class_freq[None,:])/
                valid_class_freq[None,:])
            current_iter_posterior_probs = (
                current_iter_posterior_probs_unnorm/
                np.sum(current_iter_posterior_probs_unnorm,axis=-1)[:,None])

            next_iter_class_freq = np.mean(
                current_iter_posterior_probs, axis=0) 
            iter_number += 1
        if (self.verbose):
            print("Finished on iteration",iter_number,"with delta",
                  np.sum(np.abs(current_iter_class_freq-
                                next_iter_class_freq)))
        current_iter_class_freq = next_iter_class_freq
        if (self.verbose):
            print("Final freq", current_iter_class_freq)
            print("Multiplier:",current_iter_class_freq/valid_class_freq)

        return PriorShiftAdapterFunc(
                    multipliers=(current_iter_class_freq/valid_class_freq),
                    calibrator_func=calibrator_func)
        

class BBSEImbalanceAdapter(AbstractImbalanceAdapter):

    def __init__(self, soft=False, calibrator_factory=None, verbose=False):
        self.soft = soft
        self.calibrator_factory = calibrator_factory
        self.verbose = verbose

    def __call__(self, valid_labels, tofit_initial_posterior_probs,
                       valid_posterior_probs):

        if (self.calibrator_factory is not None):
            calibrator_func = self.calibrator_factory(
                valid_preacts=valid_posterior_probs,
                valid_labels=valid_labels,
                posterior_supplied=True) 
        else:
            calibrator_func = lambda x: x

        valid_posterior_probs =\
            calibrator_func(valid_posterior_probs)
        tofit_initial_posterior_probs =\
            calibrator_func(tofit_initial_posterior_probs)

        #hard_tofit_preds binarizes tofit_initial_posterior_probs
        # according to the argmax predictions
        hard_tofit_preds = get_hard_preds(
            softmax_preds=tofit_initial_posterior_probs)
        hard_valid_preds = get_hard_preds(
            softmax_preds=valid_posterior_probs)

        if (self.soft):
            muhat_yhat = np.mean(tofit_initial_posterior_probs, axis=0) 
        else:
            muhat_yhat = np.mean(hard_tofit_preds, axis=0) 

        #prepare the "confusion" matrix (confusingly named as confusion
        # matrices are usually normalized, but theirs isn't
        if (self.soft):
            confusion_matrix = np.mean((
                valid_posterior_probs[:,:,None]*
                valid_labels[:,None,:]), axis=0)
        else:
            confusion_matrix = np.mean((hard_valid_preds[:,:,None]*
                                        valid_labels[:,None,:]),axis=0) 
        inv_confusion = linalg.inv(confusion_matrix)
        weights = inv_confusion.dot(muhat_yhat)
        if (self.verbose):
            if (np.sum(weights < 0) > 0):
                print("Heads up - some estimated weights were negative")
        weights = 1.0*(weights*(weights >= 0)) #mask out negative weights

        return PriorShiftAdapterFunc(
                    multipliers=weights,
                    calibrator_func=calibrator_func)


#effectively a wrapper around an ImbalanceAdapter
class ShiftWeightFromImbalanceAdapter(AbstractShiftWeightEstimator):

    def __init__(self, imbalance_adapter):
        self.imbalance_adapter = imbalance_adapter 

    def __call__(self, valid_labels, tofit_initial_posterior_probs,
                      valid_posterior_probs): 
        prior_shift_adapter_func = self.imbalance_adapter(
            valid_labels=valid_labels,
            tofit_initial_posterior_probs=tofit_initial_posterior_probs,
            valid_posterior_probs=valid_posterior_probs)
        return prior_shift_adapter_func.multipliers

